import logging
import os
import sys
import time
import typing
from collections import defaultdict
from contextlib import contextmanager
from enum import Enum

import skein
import tensorflow as tf
from skein.model import FinalStatus

from . import _criteo
from ._internal import encode_fn, zip_inplace
from .env import Env

logger = logging.getLogger(__name__)


class Experiment(typing.NamedTuple):
    estimator: tf.estimator.Estimator
    train_spec: tf.estimator.TrainSpec
    eval_spec: tf.estimator.EvalSpec

    @property
    def config(self) -> tf.estimator.RunConfig:
        return self.estimator.config


ExperimentFn = typing.Callable[[], Experiment]


class TaskFlavor(Enum):
    CPU = 0
    GPU = 1


NodeLabelFn = typing.Callable[[TaskFlavor], str]


class TaskSpec(typing.NamedTuple):
    memory: int
    vcores: int
    instances: int = 1
    flavor: TaskFlavor = TaskFlavor.CPU


#: A "dummy" ``TaskSpec``.
TaskSpec.NONE = TaskSpec(0, 0, 0)


class YARNCluster:
    """Multi-node cluster running on Skein.

    The implementation allocates a service with the requested number
    of instances for each distributed TensorFlow task type. Each
    instance runs ``_dispatch_task`` which roughly does the following.

    1. Find an an available TCP port and communicate the resulting
       socket address (host/port pair) to other instances using the
       "init" barrier. This is a synchronization point which ensures
       that all tasks in the cluster are ready to talk over the
       network before the Estimator machinery attempts to initialize
       a `tf.train.MonitoredSession`.
    2. Reconstruct the cluster spec from the list of socket addresses
       accumulated by the barrier, and preempt a TensorFlow server.
    3. Start the training and synchronize on the "stop" barrier.
       The barrier compensates for the fact that "ps" tasks never
       terminate, and therefore should be killed, once all other
       tasks are finished.

    Parameters
    ----------
    env : Env
        The Python environment to deploy on the containers.

    files : dict
        Local files or directories to upload to the container.
        The keys are the target locations of the resources relative
        to the container root, while the values -- their
        corresponding local sources. Note that container root is
        appended to ``PYTHONPATH``. Therefore, any listed Python
        module a package is automatically importable.

    vars : dict
        Environment variables to forward to the containers.
    """
    def __init__(
        self,
        env: Env = Env.MINIMAL,
        files: typing.Dict[str, str] = None,
        vars: typing.Dict[str, str] = None
    ) -> None:
        self.env = env
        self.files = files or {}
        self.vars = vars or _criteo.hdfs()

    def __repr__(self) -> str:
        return f"SkeinCluster(env={self.env})"

    __str__ = __repr__

    def run(
        self,
        experiment_fn: ExperimentFn,
        *,
        task_specs: typing.Dict[str, TaskSpec],
        queue: str = "default",
        node_label_fn: NodeLabelFn = _criteo.node_label_fn
    ) -> None:
        """
        Run an experiment on YARN.

        Parameters
        ----------
        experiment_fn
            A function constructing the estimator alongside the train
            and eval specs.

        task_specs
            Resources to allocate for each task type. The keys
            must be a subset of ``"chief"``, ``"worker"``, ``"ps"``, and
            ``"evaluator"``. The minimal spec must contain at least
            ``"chief"``.

        queue
            YARN queue to use.

        node_label_fn
            A function mapping ``TaskFlavor`` to the corresponding YARN
            node label expressions.
        """
        all_task_types = {"chief", "worker", "ps", "evaluator"}
        if not task_specs.keys() <= all_task_types:
            raise ValueError(
                f"task_spec keys must be a subset of: {all_task_types}")

        # TODO: compute num_ps from the model size and the number of
        # executors. See https://stackoverflow.com/a/46080567/262432.
        task_specs = defaultdict(lambda: TaskSpec.NONE, task_specs)
        assert task_specs["evaluator"].instances <= 1
        assert task_specs["chief"].instances == 1

        task_files = {
            self.env.name: self.env.create(),
            __package__: zip_inplace(os.path.dirname(__file__))
        }

        for target, source in self.files.items():
            assert target not in task_files
            task_files[target] = (
                zip_inplace(source) if os.path.isdir(source) else source
            )

        task_env = {
            **self.vars,
            # Make Python modules/packages passed via ``self.env.files``
            # importable.
            "PYTHONPATH": ".:" + self.vars.get("PYTHONPATH", ""),
            "EXPERIMENT_FN": encode_fn(experiment_fn)
        }

        services = {}
        for task_type, task_spec in list(task_specs.items()):
            if task_spec is TaskSpec.NONE:
                continue

            # TODO: use internal PyPI for CPU-optimized TF.
            if task_spec.flavor is TaskFlavor.CPU:
                env = self.env.extended_with(
                    self.env.name + "_cpu",
                    packages=["tensorflow"])
            else:
                assert task_spec.flavor is TaskFlavor.GPU
                env = self.env.extended_with(
                    self.env.name + "_gpu",
                    packages=["tensorflow-gpu"])

            task_command = (
                f"{env.name}/bin/python -m tf_skein._dispatch_task "
                f"--num-ps={task_specs['ps'].instances} "
                f"--num-workers={task_specs['worker'].instances} "
            )

            services[task_type] = skein.Service(
                [task_command],
                skein.Resources(task_spec.memory, task_spec.vcores),
                instances=task_spec.instances,
                node_label=node_label_fn(task_spec.flavor),
                files={**task_files, env.name: env.create()},
                env=task_env)

        # TODO: experiment name?
        spec = skein.ApplicationSpec(services, queue=queue)
        security = skein.Security.from_new_directory(force=True)
        with skein.Client(security=security) as client:
            logger.info(f"Submitting experiment to {queue} queue")
            app_id = client.submit(spec)
            final_status, containers = _await_termination(client, app_id)
            logger.info(
                f"Application {app_id} finished with status {final_status}")
            for id, (state, yarn_container_logs) in sorted(containers.items()):
                logger.info(f"{id:>16} {state} {yarn_container_logs}")


def _await_termination(
    client: skein.Client,
    app_id: str,
    poll_every_secs: int = 10
):
    with _shutdown(client.connect(app_id)) as app:
        final_status = FinalStatus.UNDEFINED
        containers = {}
        try:
            while final_status is FinalStatus.UNDEFINED:
                time.sleep(poll_every_secs)
                final_status = client.application_report(app_id).final_status
                for c in app.get_containers():
                    containers[c.id] = (c.state, c.yarn_container_logs)
        except skein.exceptions.ConnectionError:
            # Yes, this probably means the daemon already shutdown,
            # and we have a stale status. However, the current API
            # does not seem to allow for a better solution.
            # See jcrist/skein#46.
            pass

        return final_status, containers


@contextmanager
def _shutdown(
    app: skein.ApplicationClient
) -> typing.ContextManager[skein.ApplicationClient]:
    try:
        yield app
    finally:
        _exc_type, exc_value, _exc_tb = sys.exc_info()
        if isinstance(exc_value, (KeyboardInterrupt, SystemExit)):
            status = "KILLED"
        elif exc_value is not None:
            status = "FAILED"
        else:
            status = "SUCCEEDED"

        try:
            app.shutdown(status)
        except skein.exceptions.ConnectionError:
            pass  # Application already down.
