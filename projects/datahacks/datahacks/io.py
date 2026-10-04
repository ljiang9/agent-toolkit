"""数据输入输出的公共管道层。

对齐 data_hacks 的设计：所有工具都是「stdin 读入 → stdout 输出」的独立子命令，
可以在 shell 管道里自由组合。这个模块负责把输入读取、数值解析、输出格式化
抽出来复用，避免每个工具重复实现。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, TextIO


class DataError(ValueError):
    """数据格式错误。消息里带上出错的行号，便于定位问题。"""


def read_values(
    stream: TextIO | None = None,
    *,
    skip_comments: bool = True,
) -> list[float]:
    """从流中读取数值列表。

    支持一行多个值（空白分隔），这与 awk '{print $NF}' 产生的输出一致。

    >>> import io
    >>> read_values(io.StringIO("1 2 3\\n4\\n"))
    [1.0, 2.0, 3.0, 4.0]
    >>> read_values(io.StringIO("# 注释\\n1\\n"))
    [1.0]
    """
    src = stream or sys.stdin
    values: list[float] = []
    for lineno, line in enumerate(src, 1):
        text = line.strip()
        if not text:
            continue
        if skip_comments and text.startswith("#"):
            continue
        for token in text.split():
            try:
                values.append(float(token))
            except ValueError as exc:
                raise DataError(
                    f"第 {lineno} 行无法解析为数值：{token!r}"
                ) from exc
    return values


def read_values_or_fail(stream: TextIO | None = None) -> list[float]:
    """同 read_values，但空输入时报错而不是静默返回空结果。

    空输入通常意味着管道接错了（比如忘了 `cat file |`），
    静默返回空列表会让下游输出无意义的 0 值。
    """
    values = read_values(stream)
    if not values:
        raise DataError("输入为空：请确认管道上游有数据")
    return values


def read_lines(stream: TextIO | None = None, *, skip_comments: bool = True) -> list[str]:
    """按行读取，跳过空行与注释行。"""
    src = stream or sys.stdin
    out = []
    for line in src:
        text = line.rstrip("\n")
        if not text.strip():
            continue
        if skip_comments and text.lstrip().startswith("#"):
            continue
        out.append(text)
    return out


def read_key_values(
    stream: TextIO | None = None,
    *,
    delimiter: str = "\t",
) -> list[tuple[str, str]]:
    """读取 key<delimiter>value 形式的数据，返回 (key, value) 列表。

    用于频次统计：输入形如 `apple\t3`，输出按 key 聚合。
    """
    src = stream or sys.stdin
    out: list[tuple[str, str]] = []
    for lineno, line in enumerate(src, 1):
        text = line.rstrip("\n")
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        if delimiter not in text:
            raise DataError(
                f"第 {lineno} 行缺少分隔符 {delimiter!r}：{text!r}"
            )
        key, _, value = text.partition(delimiter)
        out.append((key.strip(), value.strip()))
    return out


def write_json(data: Any, *, stream: TextIO | None = None) -> None:
    """以 JSON 输出到 stdout。

    缩进 2 是刻意选择：agent 消费 JSON 时人眼也要能读，
    完全紧凑会让调试变得痛苦。
    """
    out = stream or sys.stdout
    json.dump(data, out, ensure_ascii=False, indent=2, default=str)
    out.write("\n")


def write_table(rows: Iterable[Iterable[Any]], *, stream: TextIO | None = None) -> None:
    """以 TSV 输出多行结果（便于再管道给下一个工具）。"""
    out = stream or sys.stdout
    for row in rows:
        out.write("\t".join(str(cell) for cell in row))
        out.write("\n")


def fmt_number(value: float, *, precision: int = 4) -> str:
    """统一数字格式：去掉无意义的小数尾巴，整数就显示为整数。

    同时消除浮点误差的显示噪音——线性插值算出的 264.99999999999994
    对人来说是噪音，应当显示为 265 而不是一长串尾数。

    >>> fmt_number(3.0)
    '3'
    >>> fmt_number(3.14159)
    '3.1416'
    >>> fmt_number(3.14159, precision=2)
    '3.14'
    >>> fmt_number(264.99999999999994, precision=2)
    '265'
    """
    if value == int(value):
        return str(int(value))
    rounded = round(value, precision)
    # 四舍五入后又变整数（如 265.00000000000006）就按整数显示
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{precision}f}"


@dataclass
class Stats:
    """一组数值的描述统计量。

    对齐 data_hacks 的 histogram.py 输出头部：
        # NumSamples = 60; Min = 0.00; Max = 1000.00
        # Mean = 332.666667; Variance = 471056.055556; SD = 686.335236; Median 191.000000
    """

    count: int
    minimum: float
    maximum: float
    mean: float
    variance: float
    stddev: float
    median: float

    @property
    def outside_count(self) -> int:
        return 0  # 由调用方按需填入

    def to_header(self, *, outside: int = 0, precision: int = 4) -> str:
        """生成注释头部，可直接拼在图表上方。"""
        p = precision
        lines = [
            f"# NumSamples = {self.count}; "
            f"Min = {self.minimum:.{p}f}; Max = {self.maximum:.{p}f}",
            f"# Mean = {self.mean:.{p}f}; Variance = {self.variance:.{p}f}; "
            f"SD = {self.stddev:.{p}f}; Median {self.median:.{p}f}",
        ]
        if outside:
            lines.insert(1, f"# {outside} value outside of min/max")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.mean,
            "variance": self.variance,
            "stddev": self.stddev,
            "median": self.median,
        }


def compute_stats(values: list[float], *, min_bound: float | None = None,
                  max_bound: float | None = None) -> tuple[Stats, int, list[float]]:
    """计算描述统计量。

    返回 (统计量, 越界个数, 参与统计的值)。
    越界的值被排除在统计之外——这与 data_hacks 的 --min/--max 语义一致。

    用样本方差（n-1）而非总体方差（n）：这是统计学惯例，
    也是 data_hacks 的行为。
    """
    if not values:
        raise DataError("没有可统计的数值")

    kept = values
    outside = 0
    if min_bound is not None or max_bound is not None:
        lo = -float("inf") if min_bound is None else min_bound
        hi = float("inf") if max_bound is None else max_bound
        kept = [v for v in values if lo <= v <= hi]
        outside = len(values) - len(kept)

    if not kept:
        raise DataError(
            f"所有 {len(values)} 个值都落在 [{min_bound}, {max_bound}] 之外"
        )

    ordered = sorted(kept)
    n = len(kept)
    mean = sum(kept) / n
    # 样本方差：除以 n-1
    variance = sum((v - mean) ** 2 for v in kept) / (n - 1) if n > 1 else 0.0
    median = _median(ordered)
    stats = Stats(
        count=n,
        minimum=ordered[0],
        maximum=ordered[-1],
        mean=mean,
        variance=variance,
        stddev=variance ** 0.5,
        median=median,
    )
    return stats, outside, kept


def _median(ordered: list[float]) -> float:
    """中位数。偶数个取中间两个的平均（对齐 data_hacks）。"""
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def quantiles_of(values: list[float], probs: list[float]) -> list[float | None]:
    """计算分位数，使用线性插值法（与 numpy 默认的 'linear' 一致）。

    >>> quantiles_of([1, 2, 3, 4], [0.0, 0.5, 1.0])
    [1.0, 2.5, 4.0]
    >>> quantiles_of([1, 2, 3, 4], [1.5])
    [None]
    """
    if not values:
        return [None] * len(probs)
    ordered = sorted(values)
    n = len(ordered)
    out: list[float | None] = []
    for q in probs:
        if q < 0.0 or q > 1.0:
            out.append(None)
            continue
        pos = q * (n - 1)
        low = int(pos)
        high = min(low + 1, n - 1)
        frac = pos - low
        out.append(ordered[low] + (ordered[high] - ordered[low]) * frac)
    return out
