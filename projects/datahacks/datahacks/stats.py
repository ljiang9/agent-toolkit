"""直方图与频次分布。

对齐 data_hacks 的 histogram.py 输出格式：

    # NumSamples = 60; Min = 0.00; Max = 1000.00
    # Mean = 332.666667; Variance = 471056.055556; SD = 686.335236; Median 191.000000
    # each ∎ represents a count of 1
        0.0000 -   100.0000 [    28]: ∎∎∎∎ (46.67%)

线性分桶与对数分桶都支持——对数分桶对长尾分布（响应时间、文件大小）必要。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .io import DataError, Stats, compute_stats

# 原项目用的方块字符，保持视觉一致
BAR_CHAR = "∎"


@dataclass
class Bucket:
    """一个分桶区间。"""

    low: float
    high: float
    count: int

    @property
    def contains(self) -> float:
        """该桶的宽度，用于计算占比。"""
        return self.high - self.low


def linear_buckets(values: list[float], *, bins: int = 10) -> list[Bucket]:
    """等宽分桶。

    所有值相同时无法等宽，退化为单桶（否则会除零）。
    """
    if bins < 1:
        raise DataError(f"分桶数必须为正整数，收到 {bins}")
    lo, hi = min(values), max(values)
    if lo == hi:
        return [Bucket(lo, hi, len(values))]

    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = int((v - lo) / width)
        # 恰好落在最大值的要归到最后一个桶，否则会越界
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    return [
        Bucket(lo + i * width, lo + (i + 1) * width, counts[i])
        for i in range(bins)
    ]


def log_buckets(values: list[float], *, bins: int = 10) -> list[Bucket]:
    """等比（对数）分桶，适合长尾分布。

    桶边界按指数增长：宽度随量级放大，因此小值区间分得细。
    """
    if bins < 1:
        raise DataError(f"分桶数必须为正整数，收到 {bins}")
    lo, hi = min(values), max(values)
    if lo <= 0:
        raise DataError(
            f"对数分桶要求所有值为正数，最小值为 {lo}；"
            f"含 0 或负数时请改用线性分桶"
        )
    if lo == hi:
        return [Bucket(lo, hi, len(values))]

    log_lo, log_hi = math.log10(lo), math.log10(hi)
    width = (log_hi - log_lo) / bins
    counts = [0] * bins
    for v in values:
        idx = int((math.log10(v) - log_lo) / width)
        if idx >= bins:
            idx = bins - 1
        if idx < 0:
            idx = 0
        counts[idx] += 1
    return [
        Bucket(10 ** (log_lo + i * width), 10 ** (log_lo + (i + 1) * width), counts[i])
        for i in range(bins)
    ]


def render_histogram(
    values: list[float],
    *,
    bins: int = 10,
    log_scale: bool = False,
    max_bar: int = 40,
    percentage: bool = True,
    min_bound: float | None = None,
    max_bound: float | None = None,
    show_header: bool = True,
) -> str:
    """渲染文本直方图。

    参数
    ----
    max_bar:
        条形最大字符宽度。每个 ∎ 代表 `每字符计数` 个样本，
        自动缩放以适配终端宽度。
    percentage:
        为 True 时在每行末尾追加百分比（对齐原项目行为）。
    """
    if not values:
        raise DataError("没有可绘制的数据")

    stats, outside, kept = compute_stats(values, min_bound=min_bound, max_bound=max_bound)
    buckets = (
        log_buckets(kept, bins=bins) if log_scale else linear_buckets(kept, bins=bins)
    )

    peak = max(b.count for b in buckets) or 1
    # 让刻度达到最大宽度：算出「每个 ∎ 代表几个样本」，保证最长条填满 max_bar
    per_char = max(1, math.ceil(peak / max_bar))

    lines: list[str] = []
    if show_header:
        lines.append(stats.to_header(outside=outside))
        lines.append(f"# each {BAR_CHAR} represents a count of {per_char}")

    total = sum(b.count for b in buckets) or 1
    for b in buckets:
        bar = BAR_CHAR * (b.count // per_char)
        pct = f" ({100 * b.count / total:.2f}%)" if percentage else ""
        lines.append(f"{b.low:>9.4f} - {b.high:>9.4f} [{b.count:>6}]: {bar}{pct}")

    return "\n".join(lines)


# ---------- 频次分布 ----------


def frequency(
    pairs: list[tuple[str, str]],
    *,
    count_by_value: bool = False,
) -> list[tuple[str, int]]:
    """按 key 聚合计数。

    参数
    ----
    count_by_value:
        为 True 时把 value 也纳入分组键（统计「key=value」组合），
        用于分析参数共现。

    返回按计数降序、键升序排列的列表——排序稳定，输出可复现。
    """
    counts: dict[str, int] = {}
    for key, value in pairs:
        group = f"{key}={value}" if count_by_value else key
        counts[group] = counts.get(group, 0) + 1
    # 计数降序；同计数按键升序，保证输出确定性
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def render_frequency(
    counts: list[tuple[str, int]],
    *,
    total: int | None = None,
    show_percentage: bool = True,
    max_key_width: int = 40,
) -> str:
    """把频次结果渲染成对齐的表格。"""
    if not counts:
        return "(无数据)"
    denom = total or sum(c for _, c in counts)
    width = min(max(len(k) for k, _ in counts), max_key_width)
    lines = []
    for key, count in counts:
        pct = f" ({100 * count / denom:5.2f}%)" if show_percentage else ""
        key_disp = key if len(key) <= width else key[: width - 1] + "…"
        lines.append(f"{key_disp:<{width}}  {count:>8}{pct}")
    return "\n".join(lines)


# ---------- 交叉透视 ----------


def pivot(
    rows: list[tuple[str, str, str]],
) -> list[dict[str, object]]:
    """三元组 (行键, 列键, 值) 转成透视表。

    >>> pivot([("a", "x", "1"), ("a", "y", "2"), ("b", "x", "3")])
    [{'row': 'a', 'x': '1', 'y': '2'}, {'row': 'b', 'x': '3', 'y': None}]
    """
    columns: list[str] = []
    seen_rows: list[str] = []
    cells: dict[tuple[str, str], str] = {}

    for row_key, col_key, value in rows:
        if col_key not in columns:
            columns.append(col_key)
        if row_key not in seen_rows:
            seen_rows.append(row_key)
        cells[(row_key, col_key)] = value

    # 行按字典序，保证输出确定
    return [
        {"row": r, **{c: cells.get((r, c)) for c in columns}}
        for r in sorted(seen_rows)
    ]
