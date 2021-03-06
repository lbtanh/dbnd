from __future__ import absolute_import, division, print_function, unicode_literals

import contextlib
import logging
import typing

from airflow import DAG
from airflow.configuration import conf as airflow_conf
from airflow.executors import LocalExecutor, SequentialExecutor
from airflow.models import DagPickle, DagRun, TaskInstance
from airflow.utils import timezone
from airflow.utils.db import provide_session
from airflow.utils.state import State
from sqlalchemy.orm import Session

from dbnd._core.constants import UpdateSource
from dbnd._core.errors import friendly_error
from dbnd._core.plugin.dbnd_plugins import assert_plugin_enabled
from dbnd._core.settings import DatabandSettings, RunConfig
from dbnd._core.task_executor.task_executor import TaskExecutor
from dbnd._core.utils.basics.pickle_non_pickable import ready_for_pickle
from dbnd_airflow.config import AirflowConfig, get_dbnd_default_args
from dbnd_airflow.db_utils import remove_listener_by_name
from dbnd_airflow.dbnd_task_executor.airflow_operator_as_dbnd import (
    AirflowDagAsDbndTask,
    AirflowOperatorAsDbndTask,
)
from dbnd_airflow.dbnd_task_executor.converters import operator_to_to_dbnd_task_id
from dbnd_airflow.dbnd_task_executor.dbnd_task_to_airflow_operator import (
    build_dbnd_operator_from_taskrun,
    set_af_operator_doc_md,
)
from dbnd_airflow.executors import AirflowTaskExecutorType
from dbnd_airflow.executors.simple_executor import InProcessExecutor
from dbnd_airflow.scheduler.single_dag_run_job import SingleDagRunJob


if typing.TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DAG_UNPICKABLE_PROPERTIES = (
    "_log",
    ("user_defined_macros", {}),
    ("user_defined_filters", {}),
    ("params", {}),
)


@provide_session
def create_dagrun_from_dbnd_run(
    databand_run,
    dag,
    execution_date,
    state=State.RUNNING,
    external_trigger=False,
    conf=None,
    session=None,
):
    """
    Create new DagRun and all relevant TaskInstances
    """
    dagrun = (
        session.query(DagRun)
        .filter(DagRun.dag_id == dag.dag_id, DagRun.execution_date == execution_date)
        .first()
    )
    if dagrun is None:
        dagrun = DagRun(
            run_id=databand_run.run_id,
            execution_date=execution_date,
            start_date=dag.start_date,
            _state=state,
            external_trigger=external_trigger,
            dag_id=dag.dag_id,
            conf=conf,
        )
        session.add(dagrun)
    else:
        logger.warning("Running with existing airflow dag run %s", dagrun)

    dagrun.dag = dag
    dagrun.run_id = databand_run.run_id
    session.commit()

    # create the associated task instances
    # state is None at the moment of creation

    # dagrun.verify_integrity(session=session)
    # fetches [TaskInstance] again
    # tasks_skipped = databand_run.tasks_skipped

    # we can find a source of the completion, but also,
    # sometimes we don't know the source of the "complete"
    TI = TaskInstance
    tis = (
        session.query(TI)
        .filter(TI.dag_id == dag.dag_id, TI.execution_date == execution_date)
        .all()
    )
    tis = {ti.task_id: ti for ti in tis}

    for af_task in dag.tasks:
        ti = tis.get(af_task.task_id)
        if ti is None:
            ti = TaskInstance(af_task, execution_date=execution_date)
            ti.start_date = timezone.utcnow()
            ti.end_date = timezone.utcnow()
            session.add(ti)
        task_run = databand_run.get_task_run_by_af_id(af_task.task_id)
        # all tasks part of the backfill are scheduled to dagrun
        if task_run.is_reused:
            # this task is completed and we don't need to run it anymore
            ti.state = State.SUCCESS

    session.commit()

    return dagrun


class AirflowTaskExecutor(TaskExecutor):
    def __init__(self, run, task_executor_type, host_engine, target_engine, task_runs):
        super(AirflowTaskExecutor, self).__init__(
            run=run,
            task_executor_type=task_executor_type,
            host_engine=host_engine,
            target_engine=target_engine,
            task_runs=task_runs,
        )

        from dbnd_airflow.bootstrap import dbnd_airflow_bootstrap

        dbnd_airflow_bootstrap()

        self.airflow_config = AirflowConfig()
        self.airflow_task_executor = self._get_airflow_executor()
        logger.info(
            "Using airflow executor: %s" % self.airflow_task_executor.__class__.__name__
        )

    def build_airflow_dag(self, task_runs):
        # create new dag from current tasks and tasks selected to run
        root_task = self.run.root_task_run.task
        if isinstance(root_task, AirflowDagAsDbndTask):
            # it's the dag without the task itself
            dag = root_task.dag
            set_af_doc_md(self.run, dag)
            for af_task in dag.tasks:
                task_run = self.run.get_task_run(operator_to_to_dbnd_task_id(af_task))
                set_af_operator_doc_md(task_run, af_task)
            return root_task.dag

        # paused is just for better clarity in the airflow ui
        dag = DAG(
            self.run.dag_id,
            default_args=get_dbnd_default_args(),
            is_paused_upon_creation=True,
            concurrency=self.airflow_config.dbnd_dag_concurrency,
        )
        with dag:
            airflow_ops = {}
            for task_run in task_runs:
                task = task_run.task
                if isinstance(task, AirflowOperatorAsDbndTask):
                    op = task.airflow_op
                    # this is hack, we clean the state of the op.
                    # better : implement proxy object like
                    # databandOperator that can wrap real Operator
                    op._dag = dag
                    op.upstream_task_ids.clear()
                    dag.add_task(op)
                    set_af_operator_doc_md(task_run, op)
                else:
                    # we will create DatabandOperator for databand tasks
                    op = build_dbnd_operator_from_taskrun(task_run)

                airflow_ops[task.task_id] = op

            for task_run in task_runs:
                task = task_run.task
                op = airflow_ops[task.task_id]
                upstream_tasks = task.ctrl.task_dag.upstream
                for t in upstream_tasks:
                    if t.task_id not in airflow_ops:
                        # we have some tasks that were not selected to run, we don't add them to graph
                        continue
                    upstream_ops = airflow_ops[t.task_id]
                    if upstream_ops.task_id not in op.upstream_task_ids:
                        op.set_upstream(upstream_ops)

        dag.fileloc = root_task.task_definition.task_source_file
        set_af_doc_md(self.run, dag)
        return dag

    def do_run(self):
        dag = self.build_airflow_dag(task_runs=self.task_runs)
        with set_dag_as_current(dag):
            from dbnd.api.tracking_api import AirflowTaskInfo

            af_instances = []
            for task_run in self.task_runs:
                if not task_run.is_reused:
                    # we build airflow infos only for not reused tasks
                    af_instance = AirflowTaskInfo(
                        execution_date=self.run.execution_date,
                        dag_id=dag.dag_id,
                        task_id=task_run.task_af_id,
                        task_run_attempt_uid=task_run.task_run_attempt_uid,
                    )
                    af_instances.append(af_instance)

            if af_instances and self.airflow_config.webserver_url:
                self.run.tracker.tracking_store.save_airflow_task_infos(
                    airflow_task_infos=af_instances,
                    source=UpdateSource.dbnd,
                    base_url=self.airflow_config.webserver_url,
                )

            self.run_airflow_dag(dag)

    def _pickle_dag_and_save_pickle_id_for_versioned(self, dag, session):
        dp = DagPickle(dag=dag)

        # First step: we need pickle id, so we save none and "reserve" pickle id
        dag.last_pickled = timezone.utcnow()
        dp.pickle = None
        session.add(dp)
        session.commit()

        # Second step: now we have pickle_id , we can add it to Operator config
        # dag_pickle_id used for Versioned Dag via TaskInstance.task_executor <- Operator.task_executor
        dag.pickle_id = dp.id
        for op in dag.tasks:
            if op.executor_config is None:
                op.executor_config = {}
            op.executor_config["DatabandExecutor"] = {
                "dbnd_driver_dump": str(self.run.driver_dump),
                "dag_pickle_id": dag.pickle_id,
                "remove_airflow_std_redirect": self.airflow_config.remove_airflow_std_redirect,
            }

        # now we are ready to create real pickle for the dag
        with ready_for_pickle(dag, DAG_UNPICKABLE_PROPERTIES) as pickable_dag:
            dp.pickle = pickable_dag
            session.add(dp)
            session.commit()

        dag.pickle_id = dp.id
        dag.last_pickled = timezone.utcnow()

    @provide_session
    def run_airflow_dag(self, dag, session=None):
        # type:  (DAG, Session) -> None
        af_dag = dag
        databand_run = self.run
        databand_context = databand_run.context
        execution_date = databand_run.execution_date
        s = databand_context.settings  # type: DatabandSettings
        s_run = s.run  # type: RunConfig

        if self.airflow_config.disable_db_ping_on_connect:
            from airflow import settings as airflow_settings

            try:
                remove_listener_by_name(
                    airflow_settings.engine, "engine_connect", "ping_connection"
                )
            except Exception as ex:
                logger.warning("Failed to optimize DB access: %s" % ex)

        if isinstance(self.airflow_task_executor, InProcessExecutor):
            heartrate = 0
        else:
            # we are in parallel mode
            heartrate = airflow_conf.getfloat("scheduler", "JOB_HEARTBEAT_SEC")

        # "Amount of time in seconds to wait when the limit "
        # "on maximum active dag runs (max_active_runs) has "
        # "been reached before trying to execute a dag run "
        # "again.
        delay_on_limit = 1.0

        self._pickle_dag_and_save_pickle_id_for_versioned(af_dag, session=session)
        af_dag.sync_to_db(session=session)

        # let create relevant TaskInstance, so SingleDagRunJob will run them
        create_dagrun_from_dbnd_run(
            databand_run=databand_run,
            dag=af_dag,
            execution_date=execution_date,
            session=session,
            state=State.RUNNING,
            external_trigger=False,
        )

        self.airflow_task_executor.fail_fast = s_run.fail_fast
        # we don't want to be stopped by zombie jobs/tasks
        airflow_conf.set("core", "dag_concurrency", str(10000))
        airflow_conf.set("core", "max_active_runs_per_dag", str(10000))

        job = SingleDagRunJob(
            dag=af_dag,
            execution_date=databand_run.execution_date,
            mark_success=s_run.mark_success,
            executor=self.airflow_task_executor,
            donot_pickle=(
                s_run.donot_pickle or airflow_conf.getboolean("core", "donot_pickle")
            ),
            ignore_first_depends_on_past=s_run.ignore_first_depends_on_past,
            ignore_task_deps=s_run.ignore_dependencies,
            fail_fast=s_run.fail_fast,
            pool=s_run.pool,
            delay_on_limit_secs=delay_on_limit,
            verbose=s.system.verbose,
            heartrate=heartrate,
            airflow_config=self.airflow_config,
        )

        # we need localDagJob to be available from "internal" functions
        # because of ti_state_manager use
        from dbnd._core.current import is_verbose

        with SingleDagRunJob.new_context(
            _context=job, allow_override=True, verbose=is_verbose()
        ):
            job.run()

    def _get_airflow_executor(self):
        """Creates a new instance of the configured executor if none exists and returns it"""
        if self.task_executor_type == AirflowTaskExecutorType.airflow_inprocess:
            return InProcessExecutor()

        if (
            self.task_executor_type
            == AirflowTaskExecutorType.airflow_multiprocess_local
        ):
            if self.run.context.settings.run.parallel:
                return LocalExecutor()
            else:
                return SequentialExecutor()

        if self.task_executor_type == AirflowTaskExecutorType.airflow_kubernetes:
            assert_plugin_enabled("dbnd-docker")

            from dbnd_airflow.executors.kubernetes_executor import (
                DbndKubernetesExecutor,
            )
            from dbnd_docker.kubernetes.kubernetes_engine_config import (
                KubernetesEngineConfig,
            )

            if not isinstance(self.target_engine, KubernetesEngineConfig):
                raise friendly_error.executor_k8s.kubernetes_with_non_compatible_engine(
                    self.target_engine
                )
            kube_dbnd = self.target_engine.build_kube_dbnd()
            if kube_dbnd.engine_config.debug:
                logging.getLogger("airflow.contrib.kubernetes").setLevel(logging.DEBUG)

            return DbndKubernetesExecutor(kube_dbnd=kube_dbnd)


def set_af_doc_md(run, dag):
    dag.doc_md = (
        "### Databand Info\n"
        "* **Tracker**: [{0}]({0})\n"
        "* **Run Name**: {1}\n"
        "* **Run UID**: {2}\n".format(run.run_url, run.name, run.run_uid)
    )


@contextlib.contextmanager
def set_dag_as_current(dag):
    """
    replace current dag of the task with the current one
    operator can have different dag if we rerun task
    :param dag:
    :return:
    """
    task_original_dag = {}
    try:
        # money time  : we are running dag. let fix all tasks dags
        # in case tasks didn't have a proper dag
        for af_task in dag.tasks:
            task_original_dag[af_task.task_id] = af_task.dag
            af_task._dag = dag
        yield dag
    finally:
        for af_task in dag.tasks:
            original_dag = task_original_dag.get(af_task.task_id)
            if original_dag:
                af_task._dag = original_dag
