from typing import Any, Dict, Optional, Sequence, Tuple, Union

import attr


# keep it below VALUE_PREVIEW_MAX_LEN at web
_DEFAULT_VALUE_PREVIEW_MAX_LEN = 10000


@attr.s(slots=True)
class ValueMeta(object):
    value_preview = attr.ib()  # type: str
    data_dimensions = attr.ib()  # type: Optional[Sequence[int]]
    data_schema = attr.ib()  # type: Optional[Dict[str,Any]]
    data_hash = attr.ib()  # type: Optional[str]
    descriptive_stats = attr.ib(
        default=None
    )  # type: Optional[Dict[str, Dict[str, Union[int, float]]]]
    histograms = attr.ib(default=None)  # type: Optional[Dict[str, Tuple]]


@attr.s
class ValueMetaConf(object):
    log_df_hist = attr.ib(default=False)  # type: Optional[bool]
    log_preview = attr.ib(default=None)  # type: Optional[bool]
    log_preview_size = attr.ib(default=None)  # type: Optional[int]
    log_schema = attr.ib(default=None)  # type: Optional[bool]
    log_size = attr.ib(default=None)  # type: Optional[bool]
    log_stats = attr.ib(default=None)  # type: Optional[bool]

    def get_preview_size(self):
        return self.log_preview_size or _DEFAULT_VALUE_PREVIEW_MAX_LEN

    @classmethod
    def enabled(cls, log_stats=False):
        return ValueMetaConf(
            log_size=True,
            log_preview=True,
            log_schema=True,
            log_stats=log_stats,
            log_df_hist=True,
        )
