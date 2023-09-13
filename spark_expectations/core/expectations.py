import functools
from dataclasses import dataclass
from typing import Dict, Optional, Any, Union, List

from pyspark.sql import DataFrame

from spark_expectations import _log
from spark_expectations.config.user_config import Constants as user_config
from spark_expectations.core import get_spark_session
from spark_expectations.core.context import SparkExpectationsContext
from spark_expectations.core.exceptions import (
    SparkExpectationsMiscException,
    SparkExpectationsDataframeNotReturnedException,
)
from spark_expectations.notifications.push.spark_expectations_notify import (
    SparkExpectationsNotify,
)
from spark_expectations.sinks.utils.collect_statistics import (
    SparkExpectationsCollectStatistics,
)
from spark_expectations.sinks.utils.writer import SparkExpectationsWriter
from spark_expectations.utils.actions import SparkExpectationsActions
from spark_expectations.utils.reader import SparkExpectationsReader
from spark_expectations.utils.regulate_flow import SparkExpectationsRegulateFlow


@dataclass
class SparkExpectations:
    """
    This class implements/supports running the data quality rules on a dataframe returned by a function

    Args:
        product_id: Name of the product
        rules_table: Name of the table which contains the rules
        stats_table: Name of the table where the stats/audit-info need to be written
        debugger: Mark it as "True" if the debugger mode need to be enabled, by default is False
        stats_streaming_options: Provide options to override the defaults, while writing into the stats streaming table
    """

    product_id: str
    rules_table: str
    stats_table: str
    debugger: bool = False
    stats_streaming_options: Optional[Dict[str, str]] = None

    def __post_init__(self) -> None:
        self.spark = get_spark_session()
        self.actions = SparkExpectationsActions()
        self._context = SparkExpectationsContext(product_id=self.product_id)

        self._writer = SparkExpectationsWriter(
            product_id=self.product_id, _context=self._context
        )
        self._process = SparkExpectationsRegulateFlow(product_id=self.product_id)
        self._notification: SparkExpectationsNotify = SparkExpectationsNotify(
            product_id=self.product_id, _context=self._context
        )
        self._statistics_decorator = SparkExpectationsCollectStatistics(
            product_id=self.product_id,
            _context=self._context,
            _writer=self._writer,
        )

        self.reader = SparkExpectationsReader(
            product_id=self.product_id,
            _context=self._context,
        )

        self._context.set_debugger_mode(self.debugger)

    def with_expectations(
        self,
        target_table: str,
        write_to_table: bool = False,
        write_to_temp_table: bool = False,
        spark_conf: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, str]] = None,
        options_error_table: Optional[Dict[str, str]] = None,
        rules_table: Optional[str] = None,
        stats_table: Optional[str] = None,
        target_table_view: Optional[str] = None,
        actions_if_failed: Optional[List[str]] = None,
    ) -> Any:
        """
        This decorator helps to wrap a function which returns dataframe and apply dataframe rules on it

        Args:
            target_table: Name of the table where the final dataframe need to be written
            write_to_table: Mark it as "True" if the dataframe need to be written as table
            write_to_temp_table: Mark it as "True" if the input dataframe need to be written to the temp table to break
                                the spark plan
            spark_conf: Provide SparkConf to override the defaults, while writing into the table & which also contains
            notifications related variables
            options: Provide Options to override the defaults, while writing into the table
            options_error_table: Provide options to override the defaults, while writing into the error table
            rules_table: Name of the table which contains the rules
            stats_table: Name of the table where the stats/audit-info need to be written
            target_table_view: This view is created after the _row_dq process to run the target agg_dq and query_dq.
                If value is not provided, defaulted to {target_table}_view
            actions_if_failed: Provide the list of actions to be taken if the expectations failed. Default would be all
                actions ['ignore', 'drop', 'fail']


        Returns:
            Any: Returns a function which applied the expectations on dataset
        """

        def _except(func: Any) -> Any:
            # variable used for enabling notification at different level
            _default_notification_dict: Dict[str, Union[str, int, bool]] = {
                user_config.se_notifications_on_start: False,
                user_config.se_notifications_on_completion: False,
                user_config.se_notifications_on_fail: True,
                user_config.se_notifications_on_error_drop_exceeds_threshold_breach: False,
                user_config.se_notifications_on_error_drop_threshold: 100,
            }
            _notification_dict: Dict[str, Union[str, int, bool]] = (
                {**_default_notification_dict, **spark_conf}
                if spark_conf
                else _default_notification_dict
            )

            _default_stats_streaming_dict: Dict[str, Union[bool, str]] = {
                user_config.se_enable_streaming: True,
                user_config.secret_type: "databricks",
                user_config.dbx_workspace_url: "https://workspace.cloud.databricks.com",
                user_config.dbx_secret_scope: "sole_common_prod",
                user_config.dbx_kafka_server_url: "se_streaming_server_url_secret_key",
                user_config.dbx_secret_token_url: "se_streaming_auth_secret_token_url_key",
                user_config.dbx_secret_app_name: "se_streaming_auth_secret_appid_key",
                user_config.dbx_secret_token: "se_streaming_auth_secret_token_key",
                user_config.dbx_topic_name: "se_streaming_topic_name",
            }

            _se_stats_streaming_dict: Dict[str, Any] = (
                {**self.stats_streaming_options}
                if self.stats_streaming_options
                else _default_stats_streaming_dict
            )

            # need to call the get_rules_frm_table function to get the rules from the table as expectations
            expectations, rules_execution_settings = self.reader.get_rules_from_table(
                self.rules_table if rules_table is None else rules_table,
                self.stats_table if stats_table is None else stats_table,
                target_table,
                actions_if_failed,
            )

            _row_dq: bool = rules_execution_settings.get("row_dq", False)
            _agg_dq: bool = rules_execution_settings.get("agg_dq", False)
            _source_agg_dq: bool = rules_execution_settings.get("source_agg_dq", False)
            _target_agg_dq: bool = rules_execution_settings.get("target_agg_dq", False)
            _query_dq: bool = rules_execution_settings.get("query_dq", False)
            _source_query_dq: bool = rules_execution_settings.get(
                "source_query_dq", False
            )
            _target_query_dq: bool = rules_execution_settings.get(
                "target_query_dq", False
            )
            _target_table_view: str = (
                target_table_view if target_table_view else f"{target_table}_view"
            )

            _notification_on_start: bool = (
                bool(_notification_dict[user_config.se_notifications_on_start])
                if isinstance(
                    _notification_dict[user_config.se_notifications_on_start],
                    bool,
                )
                else False
            )
            _notification_on_completion: bool = (
                bool(_notification_dict[user_config.se_notifications_on_completion])
                if isinstance(
                    _notification_dict[user_config.se_notifications_on_completion],
                    bool,
                )
                else False
            )
            _notification_on_fail: bool = (
                bool(_notification_dict[user_config.se_notifications_on_fail])
                if isinstance(
                    _notification_dict[user_config.se_notifications_on_fail],
                    bool,
                )
                else False
            )
            _notification_on_error_drop_exceeds_threshold_breach: bool = (
                bool(
                    _notification_dict[
                        user_config.se_notifications_on_error_drop_exceeds_threshold_breach
                    ]
                )
                if isinstance(
                    _notification_dict[
                        user_config.se_notifications_on_error_drop_exceeds_threshold_breach
                    ],
                    bool,
                )
                else False
            )
            _error_drop_threshold: int = (
                int(
                    _notification_dict[
                        user_config.se_notifications_on_error_drop_threshold
                    ]
                )
                if isinstance(
                    _notification_dict[
                        user_config.se_notifications_on_error_drop_threshold
                    ],
                    int,
                )
                else 100
            )

            self.reader.set_notification_param(spark_conf)
            self._context.set_notification_on_start(_notification_on_start)
            self._context.set_notification_on_completion(_notification_on_completion)
            self._context.set_notification_on_fail(_notification_on_fail)

            self._context.set_se_streaming_stats_dict(_se_stats_streaming_dict)

            @self._notification.send_notification_decorator
            @self._statistics_decorator.collect_stats_decorator
            @functools.wraps(func)
            def wrapper(*args: tuple, **kwargs: dict) -> DataFrame:
                try:
                    _log.info("The function dataframe is getting created")
                    # _df: DataFrame = func(*args, **kwargs)
                    _df: DataFrame = func(*args, **kwargs)
                    table_name: str = self._context.get_table_name

                    _input_count = _df.count()
                    _output_count: int = 0
                    _error_count: int = 0
                    _source_dq_df: Optional[DataFrame] = None
                    _source_query_dq_df: Optional[DataFrame] = None
                    _row_dq_df: Optional[DataFrame] = None
                    _final_dq_df: Optional[DataFrame] = None
                    _final_query_dq_df: Optional[DataFrame] = None

                    # initialize variable with default values through set
                    self._context.set_dq_run_status()
                    self._context.set_source_agg_dq_status()
                    self._context.set_source_query_dq_status()
                    self._context.set_row_dq_status()
                    self._context.set_final_agg_dq_status()
                    self._context.set_final_query_dq_status()
                    self._context.set_input_count()
                    self._context.set_error_count()
                    self._context.set_output_count()
                    self._context.set_source_agg_dq_result()
                    self._context.set_final_agg_dq_result()
                    self._context.set_source_query_dq_result()
                    self._context.set_final_query_dq_result()
                    self._context.set_summarised_row_dq_res()

                    # initialize variables of start and end time with default values
                    self._context._source_agg_dq_start_time = None
                    self._context._final_agg_dq_start_time = None
                    self._context._source_query_dq_start_time = None
                    self._context._final_query_dq_start_time = None
                    self._context._row_dq_start_time = None

                    self._context._source_agg_dq_end_time = None
                    self._context._final_agg_dq_end_time = None
                    self._context._source_query_dq_end_time = None
                    self._context._final_query_dq_end_time = None
                    self._context._row_dq_end_time = None

                    self._context.set_input_count(_input_count)
                    self._context.set_error_drop_threshold(_error_drop_threshold)

                    if isinstance(_df, DataFrame):
                        _log.info("The function dataframe is created")
                        self._context.set_table_name(table_name)
                        if write_to_temp_table:
                            _log.info("Dropping to temp table started")
                            self.spark.sql(f"drop table if exists {table_name}_temp")
                            _log.info("Dropping to temp table completed")
                            _log.info("Writing to temp table started")
                            self._writer.write_df_to_table(
                                _df,
                                f"{table_name}_temp",
                                spark_conf=spark_conf,
                                options=options,
                            )
                            _log.info("Read from temp table started")
                            _df = self.spark.sql(f"select * from {table_name}_temp")
                            _log.info("Read from temp table completed")

                        func_process = self._process.execute_dq_process(
                            _context=self._context,
                            _actions=self.actions,
                            _writer=self._writer,
                            _notification=self._notification,
                            expectations=expectations,
                            table_name=table_name,
                            _input_count=_input_count,
                            spark_conf=spark_conf,
                            options_error_table=options_error_table,
                        )

                        if _agg_dq is True and _source_agg_dq is True:
                            _log.info(
                                "started processing data quality rules for agg level expectations on soure dataframe"
                            )
                            self._context.set_source_agg_dq_status("Failed")
                            self._context.set_source_agg_dq_start_time()
                            # In this steps source agg data quality expectations runs on raw_data
                            # returns:
                            #        _source_dq_df: applied data quality dataframe,
                            #        _dq_source_agg_results: source aggregation result in dictionary
                            #        _: place holder for error data at row level
                            #        status: status of the execution

                            (
                                _source_dq_df,
                                _dq_source_agg_results,
                                _,
                                status,
                            ) = func_process(
                                _df,
                                self._context.get_agg_dq_rule_type_name,
                                source_agg_dq_flag=True,
                            )
                            self._context.set_source_agg_dq_status(status)
                            self._context.set_source_agg_dq_end_time()

                            _log.info(
                                "ended processing data quality rules for agg level expectations on source dataframe"
                            )

                        if _query_dq is True and _source_query_dq is True:
                            _log.info(
                                "started processing data quality rules for query level expectations on soure dataframe"
                            )
                            self._context.set_source_query_dq_status("Failed")
                            self._context.set_source_query_dq_start_time()
                            # In this steps source query data quality expectations runs on raw_data
                            # returns:
                            #        _source_query_dq_df: applied data quality dataframe,
                            #        _dq_source_query_results: source query dq results in dictionary
                            #        _: place holder for error data at row level
                            #        status: status of the execution

                            (
                                _source_query_dq_df,
                                _dq_source_query_results,
                                _,
                                status,
                            ) = func_process(
                                _df,
                                self._context.get_query_dq_rule_type_name,
                                source_query_dq_flag=True,
                            )
                            self._context.set_source_query_dq_status(status)
                            self._context.set_source_query_dq_end_time()
                            _log.info(
                                "ended processing data quality rules for query level expectations on source dataframe"
                            )

                        if _row_dq is True:
                            _log.info(
                                "started processing data quality rules for row level expectations"
                            )
                            self._context.set_row_dq_status("Failed")
                            self._context.set_row_dq_start_time()
                            # In this steps row level data quality expectations runs on raw_data
                            # returns:
                            #        _row_dq_df: applied data quality dataframe at row level on raw dataframe,
                            #        _: place holder for aggregation
                            #        _error_count: number of error records
                            #        status: status of the execution
                            (_row_dq_df, _, _error_count, status) = func_process(
                                _df,
                                self._context.get_row_dq_rule_type_name,
                                row_dq_flag=True,
                            )
                            self._context.set_error_count(_error_count)

                            if _target_table_view:
                                if _row_dq_df:
                                    _row_dq_df.createOrReplaceTempView(
                                        _target_table_view
                                    )

                            _output_count = _row_dq_df.count() if _row_dq_df else 0
                            self._context.set_output_count(_output_count)

                            self._context.set_row_dq_status(status)
                            self._context.set_row_dq_end_time()

                            if (
                                _notification_on_error_drop_exceeds_threshold_breach
                                is True
                                and (100 - self._context.get_output_percentage)
                                >= _error_drop_threshold
                            ):
                                self._notification.notify_on_exceeds_of_error_threshold()
                                # raise SparkExpectationsErrorThresholdExceedsException(
                                #     "An error has taken place because"
                                #     " the set limit for acceptable"
                                #     " errors, known as the error"
                                #     " threshold, has been surpassed"
                                # )
                            _log.info(
                                "ended processing data quality rules for row level expectations"
                            )

                        if (
                            _row_dq is True
                            and _agg_dq is True
                            and _target_agg_dq is True
                        ):
                            _log.info(
                                "started processing data quality rules for agg level expectations on final dataframe"
                            )
                            self._context.set_final_agg_dq_status("Failed")
                            self._context.set_final_agg_dq_start_time()
                            # In this steps final agg data quality expectations run on final dataframe
                            # returns:
                            #        _final_dq_df: applied data quality dataframe at row level on raw dataframe,
                            #        _dq_final_agg_results: final agg dq result in dictionary
                            #        _: number of error records
                            #        status: status of the execution
                            (
                                _final_dq_df,
                                _dq_final_agg_results,
                                _,
                                status,
                            ) = func_process(
                                _row_dq_df,
                                self._context.get_agg_dq_rule_type_name,
                                final_agg_dq_flag=True,
                                error_count=_error_count,
                                output_count=_output_count,
                            )
                            self._context.set_final_agg_dq_status(status)
                            self._context.set_final_agg_dq_end_time()
                            _log.info(
                                "ended processing data quality rules for agg level expectations on final dataframe"
                            )

                        if (
                            _row_dq is True
                            and _query_dq is True
                            and _target_query_dq is True
                        ):
                            _log.info(
                                "started processing data quality rules for query level expectations on final dataframe"
                            )
                            self._context.set_final_query_dq_status("Failed")
                            self._context.set_final_query_dq_start_time()
                            # In this steps final query dq data quality expectations run on final dataframe
                            # returns:
                            #        _final_query_dq_df: applied data quality dataframe at row level on raw dataframe,
                            #        _dq_final_query_results: final query dq result in dictionary
                            #        _: number of error records
                            #        status: status of the execution

                            if _target_table_view and _row_dq_df:
                                _row_dq_df.createOrReplaceTempView(_target_table_view)
                            else:
                                raise SparkExpectationsMiscException(
                                    "final table view name is not supplied to run query dq"
                                )

                            (
                                _final_query_dq_df,
                                _dq_final_query_results,
                                _,
                                status,
                            ) = func_process(
                                _row_dq_df,
                                self._context.get_query_dq_rule_type_name,
                                final_query_dq_flag=True,
                                error_count=_error_count,
                                output_count=_output_count,
                            )
                            self._context.set_final_query_dq_status(status)
                            self._context.set_final_query_dq_end_time()

                            _log.info(
                                "ended processing data quality rules for query level expectations on final dataframe"
                            )

                        if _row_dq and write_to_table:
                            _log.info("Writing into the final table started")
                            self._writer.write_df_to_table(
                                _row_dq_df,
                                f"{table_name}",
                                spark_conf=spark_conf,
                                options=options,
                            )
                            _log.info("Writing into the final table ended")

                    else:
                        raise SparkExpectationsDataframeNotReturnedException(
                            "error occurred while processing spark "
                            "expectations due to given dataframe is not type of dataframe"
                        )
                    self.spark.catalog.clearCache()

                    return _row_dq_df

                except Exception as e:
                    raise SparkExpectationsMiscException(
                        f"error occurred while processing spark expectations {e}"
                    )

            return wrapper

        return _except
