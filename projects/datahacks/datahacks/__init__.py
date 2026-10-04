"""datahacks —— 纯标准库实现的命令行数据分析工具集。

复刻自 Bitly 的 data_hacks（https://github.com/bitly/data_hacks），
把六个独立的命令行小工具整合为一套可组合的统计原语库，
并额外提供 agent 工具接入层。

所有工具都是 stdin → stdout 的管道友好设计：

    cat access.log | datahacks histogram --bins 20
    awk '{print $NF}' log | datahacks quantile --probs 0.5 0.95

>>> from datahacks import render_histogram, compute_stats
>>> stats, outside, kept = compute_stats([1.0, 2.0, 3.0, 4.0])
>>> stats.median
2.5
>>> "NumSamples" in render_histogram([1.0, 2.0, 3.0])
True
"""

from __future__ import annotations

from .io import (
    DataError,
    Stats,
    compute_stats,
    fmt_number,
    quantiles_of,
    read_key_values,
    read_lines,
    read_values,
    write_json,
    write_table,
)
from .sample import (
    hash_file,
    hash_lines,
    percentile,
    sample_lines,
    unique_count,
    unique_lines,
)
from .stats import (
    BAR_CHAR,
    Bucket,
    frequency,
    linear_buckets,
    log_buckets,
    pivot,
    render_frequency,
    render_histogram,
)

__version__ = "0.1.0"

__all__ = [
    "BAR_CHAR",
    "Bucket",
    "DataError",
    "Stats",
    "compute_stats",
    "fmt_number",
    "frequency",
    "hash_file",
    "hash_lines",
    "linear_buckets",
    "log_buckets",
    "percentile",
    "pivot",
    "quantiles_of",
    "read_key_values",
    "read_lines",
    "read_values",
    "render_frequency",
    "render_histogram",
    "sample_lines",
    "unique_count",
    "unique_lines",
    "write_json",
    "write_table",
    "__version__",
]
