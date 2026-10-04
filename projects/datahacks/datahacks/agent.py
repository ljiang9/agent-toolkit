"""datahacks 的 agent 工具接入层。

每个统计原语注册为独立工具，入参统一为「数据 + 可选列名」，
出参统一为 JSON，便于 LLM 解析与后续工具链消费。
"""

from __future__ import annotations

from typing import Any

from agent_core import ToolRegistry

from .io import DataError, quantiles_of, read_key_values, read_lines
from .sample import hash_lines, percentile, sample_lines, unique_count, unique_lines
from .stats import frequency, pivot, render_frequency, render_histogram


def _parse_numbers(values: list[Any]) -> list[float]:
    """把 agent 传来的混合类型数值列表转成 float。

    宽松接受 "1.5" 这类字符串——模型经常把数字包成字符串发过来，
    直接报错会让本可成功的调用失败。
    """
    out: list[float] = []
    for v in values:
        try:
            out.append(float(v))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无法解析为数值的元素：{v!r}") from exc
    return out


def register(registry: ToolRegistry) -> None:
    """把 datahacks 的能力注册进工具注册表。"""

    @registry.tool(
        name="data_histogram",
        description=(
            "对一组数值生成文本直方图，并附描述统计（样本数/最小/最大/均值/"
            "方差/标准差/中位数）。适合快速看清数据分布，"
            "判断是均匀、长尾还是双峰。当需要了解一批数值的分布形态、"
            "或检测异常值时使用。支持线性分桶与对数分桶（对数适合响应时间"
            "这类跨数量级的数据）。"
        ),
        properties={
            "values": {
                "type": "array",
                "items": {"type": ["number", "string"], "description": "数值或数值字符串"},
                "description": "数值列表；元素也接受字符串形式（如 \"1.5\"）",
            },
            "bins": {"type": "integer", "default": 10, "description": "分桶数，默认 10"},
            "log_scale": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "是否用对数分桶，要求所有值为正",
            },
            "min_bound": {
                "type": ["number", "null"],
                "default": None,
                "description": "下界，小于此值的样本被排除在统计外",
            },
            "max_bound": {
                "type": ["number", "null"],
                "default": None,
                "description": "上界，大于此值的样本被排除在统计外",
            },
        },
    )
    def data_histogram(
        values: list[Any],
        bins: int = 10,
        log_scale: bool | None = None,
        min_bound: float | None = None,
        max_bound: float | None = None,
    ) -> dict[str, Any]:
        nums = _parse_numbers(values)
        text = render_histogram(
            nums,
            bins=bins,
            log_scale=bool(log_scale),
            min_bound=min_bound,
            max_bound=max_bound,
        )
        return {"chart": text, "lines": text.count("\n") + 1}

    @registry.tool(
        name="data_quantiles",
        description=(
            "计算一组数值的分位数（默认 P50/P90/P95/P99，使用线性插值法）。"
            "当需要 P95 响应时间这类 SLA 指标、或想知道分布的尾部情况时使用。"
            "比直方图更精确，适合直接作为报告数字。"
        ),
        properties={
            "values": {
                "type": "array",
                "items": {"type": ["number", "string"], "description": "数值或数值字符串"},
                "description": "数值列表；元素也接受字符串形式",
            },
            "probs": {
                "type": "array",
                "items": {"type": "number"},
                "default": [0.5, 0.9, 0.95, 0.99],
                "description": "分位点，取值 0-1，如 0.95 表示 P95",
            },
        },
    )
    def data_quantiles(
        values: list[Any], probs: list[float] | None = None
    ) -> dict[str, Any]:
        nums = _parse_numbers(values)
        ps = probs if probs is not None else [0.5, 0.9, 0.95, 0.99]
        results = quantiles_of(nums, ps)
        return {
            "quantiles": {
                f"P{p * 100:g}": r for p, r in zip(ps, results, strict=False)
            }
        }

    @registry.tool(
        name="data_frequency",
        description=(
            "按 key 统计出现频次并降序排列，也可按 key=value 组合统计共现。"
            "当需要找出最常见的取值（如日志里最频繁的错误码、"
            "访问最多的 URL）、或统计参数组合时使用。"
            "输出顺序稳定（计数降序、同计数按键升序），便于对比多次运行结果。"
        ),
        properties={
            "pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
                "description": "键值对列表",
            },
            "count_by_value": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "为 True 时按 key=value 组合统计",
            },
            "top": {
                "type": ["integer", "null"],
                "default": 20,
                "description": "只返回前 N 项",
            },
        },
    )
    def data_frequency(
        pairs: list[dict[str, str]],
        count_by_value: bool | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        pairs_list = [(p["key"], p.get("value", "")) for p in pairs]
        counts = frequency(pairs_list, count_by_value=bool(count_by_value))
        limit = top if top and top > 0 else len(counts)
        limited = counts[:limit]
        return {
            "total_unique": len(counts),
            "returned": len(limited),
            "table": render_frequency(limited),
            "counts": [{"key": k, "count": c} for k, c in limited],
        }

    @registry.tool(
        name="data_unique",
        description=(
            "对文本行去重并统计唯一值数量，保持首次出现的顺序。"
            "当需要清洗日志里的重复记录、或统计不同用户/IP/请求路径的数量时使用。"
            "提供 count_only 参数：大数据上只数不列可显著省内存。"
        ),
        properties={
            "lines": {"type": "array", "items": {"type": "string"},
                      "description": "文本行列表"},
            "count_only": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "只返回唯一值数量，不返回列表",
            },
            "limit": {
                "type": ["integer", "null"],
                "default": 50,
                "description": "列表模式下最多返回前 N 项",
            },
        },
    )
    def data_unique(
        lines: list[str],
        count_only: bool | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if count_only:
            return {
                "total": len(lines),
                "unique_count": unique_count(lines),
                "duplicates": len(lines) - unique_count(lines),
            }
        uniq = unique_lines(lines)
        cap = limit if limit and limit > 0 else len(uniq)
        return {
            "total": len(lines),
            "unique_count": len(uniq),
            "duplicates": len(lines) - len(uniq),
            "items": uniq[:cap],
        }

    @registry.tool(
        name="data_sample",
        description=(
            "从数据中随机抽样，可按百分比或条数。给定 seed 时结果完全可复现，"
            "适合做抽样检查或缩减大数据集以便进一步分析。"
            "当数据量太大无法全量处理、或只想抽查一部分时使用。"
        ),
        properties={
            "lines": {"type": "array", "items": {"type": "string"},
                      "description": "文本行列表"},
            "percentage": {
                "type": ["number", "null"],
                "default": None,
                "description": "抽样比例 0-100，指定后忽略 count",
            },
            "count": {
                "type": ["integer", "null"],
                "default": None,
                "description": "抽取条数",
            },
            "seed": {
                "type": ["integer", "null"],
                "default": None,
                "description": "随机种子，给定后结果可复现",
            },
        },
    )
    def data_sample(
        lines: list[str],
        percentage: float | None = None,
        count: int | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        picked = sample_lines(lines, percentage, count=count, seed=seed)
        return {
            "input_count": len(lines),
            "sampled_count": len(picked),
            "items": picked,
        }

    @registry.tool(
        name="data_hash",
        description=(
            "计算文本行的哈希（md5/sha1/sha256 等），用于去重、比对样本或校验完整性。"
            "逐行模式给每行一个哈希；整块模式把所有行合成一个哈希。"
            "当需要判断两批数据是否完全一致、或按内容而非位置做样本比对时使用。"
        ),
        properties={
            "lines": {"type": "array", "items": {"type": "string"},
                      "description": "文本行列表"},
            "algorithm": {
                "type": "string",
                "default": "md5",
                "enum": ["md5", "sha1", "sha256", "sha512", "blake2b"],
                "description": "哈希算法，默认 md5",
            },
            "per_line": {
                "type": ["boolean", "null"],
                "default": True,
                "description": "为 False 时把全部行合成一个哈希",
            },
        },
    )
    def data_hash(
        lines: list[str],
        algorithm: str = "md5",
        per_line: bool | None = None,
    ) -> dict[str, Any]:
        result = hash_lines(lines, algorithm=algorithm, per_line=bool(per_line))
        return {
            "algorithm": algorithm,
            "hashes": result,
            "count": len(result),
        }

    @registry.tool(
        name="data_percentile",
        description=(
            "计算单个百分位值（0-100）。最典型的用法是取 P95 响应时间——"
            "从访问日志里评估真实用户体验，而不是被平均值掩盖长尾。"
            "当需要回答「95% 的请求在多少毫秒内完成」这类问题时使用。"
        ),
        properties={
            "values": {
                "type": "array",
                "items": {"type": ["number", "string"], "description": "数值或数值字符串"},
                "description": "数值列表；元素也接受字符串形式",
            },
            "percentile": {"type": "number", "description": "百分位 0-100，如 95"},
        },
    )
    def data_percentile(values: list[Any], percentile: float) -> dict[str, Any]:
        nums = _parse_numbers(values)
        value = percentile_fn(nums, percentile)
        return {
            "percentile": percentile,
            "value": value,
            "count": len(nums),
        }

    @registry.tool(
        name="data_pivot",
        description=(
            "把 (行键, 列键, 值) 三元组转成交叉透视表，行列均自动排序。"
            "当需要把「日期 × 状态」「用户 × 动作」这类二维分布摊平成表格时使用。"
        ),
        properties={
            "triples": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "row": {"type": "string"},
                        "column": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["row", "column", "value"],
                    "additionalProperties": False,
                },
                "description": "(row, column, value) 三元组列表",
            },
        },
    )
    def data_pivot(triples: list[dict[str, str]]) -> dict[str, Any]:
        rows = pivot([(t["row"], t["column"], t["value"]) for t in triples])
        return {"rows": rows, "row_count": len(rows)}

    @registry.tool(
        name="data_describe",
        description=(
            "一次性算出一组数值的完整描述统计：样本数、最小值、最大值、均值、"
            "方差、标准差、中位数，以及 P25/P50/P75/P95/P99 分位数。"
            "当你拿到一批陌生数值、需要先摸清它长什么样时用这个，"
            "比逐个调用多个统计工具更省事。"
        ),
        properties={
            "values": {
                "type": "array",
                "items": {"type": ["number", "string"], "description": "数值或数值字符串"},
                "description": "数值列表；元素也接受字符串形式",
            },
        },
    )
    def data_describe(values: list[Any]) -> dict[str, Any]:
        from .io import compute_stats

        nums = _parse_numbers(values)
        stats, outside, kept = compute_stats(nums)
        qs = quantiles_of(nums, [0.25, 0.5, 0.75, 0.95, 0.99])
        return {
            **stats.to_dict(),
            "outside_bounds": outside,
            "quantiles": dict(zip(
                ["P25", "P50", "P75", "P95", "P99"], qs, strict=False
            )),
        }


# 避免与工具参数同名，把 percentile 函数换个引用
percentile_fn = percentile

__all__ = ["register"]
