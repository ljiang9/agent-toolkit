"""抽样、去重与哈希。

对齐 data_hacks 的 sample.py（按百分比过滤流），
并补上 agent 场景常用的去重与指纹去重。
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .io import DataError


def sample_lines(
    lines: Iterable[str],
    percentage: float | None = None,
    *,
    count: int | None = None,
    seed: int | None = None,
) -> list[str]:
    """从流中抽样。

    参数
    ----
    percentage:
        抽样比例，0–100。指定它时 count 被忽略。
    count:
        抽取的条数。
    seed:
        随机种子。给定后结果可复现——做测试与基准时必须给。

    percentage 与 count 都为 None 时返回全部。
    """
    items = list(lines)

    if percentage is None and count is None:
        return items
    if percentage is None and count is not None:
        if count < 0:
            raise DataError(f"抽样条数不能为负，收到 {count}")
        rng = random.Random(seed)
        return rng.sample(items, min(count, len(items)))
    if percentage is None and count is None:
        return items

    assert percentage is not None  # type narrowing
    if not 0 <= percentage <= 100:
        raise DataError(f"抽样比例必须在 0-100 之间，收到 {percentage}")
    if percentage == 100:
        return items
    if percentage == 0:
        return []

    # 按比例算条数，至少 1 条（否则小样本下抽不出东西）
    n = max(1, int(len(items) * percentage / 100))
    rng = random.Random(seed)
    return rng.sample(items, min(n, len(items)))


def unique_lines(lines: Iterable[str], *, key_func=None) -> list[str]:
    """去重，保持首次出现的顺序。

    顺序重要：日志分析里「第一次出现」往往才是你要的那条。
    """
    seen: set[object] = set()
    out: list[str] = []
    for line in lines:
        key = key_func(line) if key_func else line
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def unique_count(lines: Iterable[str], *, key_func=None) -> int:
    """只数不列——大文件上比 unique_lines 省内存。"""
    seen: set[object] = set()
    for line in lines:
        seen.add(key_func(line) if key_func else line)
    return len(seen)


# ---------- 哈希 ----------


def hash_lines(
    lines: Iterable[str],
    *,
    algorithm: str = "md5",
    per_line: bool = True,
) -> dict[str, str]:
    """计算哈希。

    参数
    ----
    per_line:
        为 True 时逐行计算（用于去重、比对样本）；
        为 False 时把所有行拼成一个整体算一个哈希（用于校验文件完整性）。
    """
    if algorithm not in hashlib.algorithms_available:
        raise DataError(
            f"不支持的哈希算法 {algorithm!r}；"
            f"可用：{sorted(hashlib.algorithms_available)}"
        )

    out: dict[str, str] = {}
    if per_line:
        for line in lines:
            h = hashlib.new(algorithm)
            # 去掉行尾换行，避免同样的内容因换行符不同得到不同哈希
            h.update(line.rstrip("\n").encode("utf-8"))
            out[line] = h.hexdigest()
    else:
        h = hashlib.new(algorithm)
        for line in lines:
            # 分隔符必须在「行内容之间」而不是只在末尾，否则
            # ["ab","c"] 与 ["a","bc"] 拼接后完全相同，仍会撞车。
            h.update(line.rstrip("\n").encode("utf-8"))
            h.update(b"\x00")
        out["__all__"] = h.hexdigest()
    return out


def hash_file(path: str, *, algorithm: str = "sha256") -> str:
    """计算文件哈希，分块读取以支持大文件。"""
    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 分位数与百分位 ----------


def percentile(
    values: list[float],
    p: float,
) -> float | None:
    """单个百分位（0-100）。

    这是 data_hacks 的 ninety_five_percent.py 的核心：找出响应时间的 P95。

    >>> percentile([1, 2, 3, 4], 95)
    3.85
    >>> percentile([1, 2, 3, 4], 150) is None
    True
    """
    from .io import quantiles_of

    if not 0 <= p <= 100:
        return None
    result = quantiles_of(values, [p / 100.0])
    return result[0]
