"""formcodec 的 agent 工具接入层。

解决的痛点：LLM 手写 `application/x-www-form-urlencoded` 请求体时，
参数拼接和类型转换极易出错（漏转义、把 int 写成 string、数组忘记加 []）。
这里把「结构体 → 表单串」与「表单串 → 结构体」做成两个互逆工具。
"""

from __future__ import annotations

from typing import Any

from agent_core import ToolRegistry

from .core import (
    decode_form,
    encode_form,
    form_to_json,
    form_to_query_string,
    unflatten,
)
from .path import KeyPath


def register(registry: ToolRegistry) -> None:
    """把 formcodec 的能力注册进工具注册表。"""

    @registry.tool(
        name="formcodec_encode",
        description=(
            "把对象编码成 application/x-www-form-urlencoded 键值对。"
            "数组会展开成重复键（tags=a&tags=b），嵌套字典用 a[b] 形式。"
            "当需要构造 HTTP 表单请求体、或把结构化意图（如「按 status=active "
            "筛选、按 created_at 倒序取 20 条」）可靠地编码成请求参数时使用。"
            "手工拼串容易漏转义或写错类型，交给这个工具更稳妥。"
        ),
        properties={
            "data": {
                "type": "object",
                "description": "要编码的对象，值可为字符串/数字/布尔/数组/嵌套对象",
            },
            "as_query_string": {
                "type": ["boolean", "null"],
                "default": True,
                "description": "为 True 返回 URL query 串，否则返回键值对数组",
            },
        },
    )
    def formcodec_encode(
        data: dict[str, Any], as_query_string: bool | None = None
    ) -> dict[str, Any]:
        pairs = encode_form(data)
        if as_query_string is None:
            as_query_string = True
        if as_query_string:
            return {
                "query_string": form_to_query_string(pairs),
                "pairs": [{"key": k, "value": v} for k, v in pairs],
            }
        return {"pairs": [{"key": k, "value": v} for k, v in pairs]}

    @registry.tool(
        name="formcodec_decode",
        description=(
            "把 application/x-www-form-urlencoded 键值对转回对象，自动做类型转换"
            "（字符串 -> 整数/浮点/布尔/列表/时间）。支持 a.b 与 a[0] 嵌套语法。"
            "当收到一个表单请求或 API 的 form-urlencoded 响应、"
            "需要把它变成规整的结构化对象时使用。"
        ),
        properties={
            "pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
                "description": "键值对列表，同名 key 可重复（数组的常规表示）",
            },
            "value_type": {
                "type": "string",
                "enum": ["dict", "flat"],
                "default": "dict",
                "description": "dict=按点号/索引展开成嵌套结构，flat=保持原始键名",
            },
        },
    )
    def formcodec_decode(
        pairs: list[dict[str, Any]], value_type: str = "dict"
    ) -> dict[str, Any]:
        raw = [(p["key"], p.get("value")) for p in pairs]
        if value_type == "flat":
            return {"data": decode_form(raw, dict)}
        # 默认展开成嵌套结构
        return {"data": unflatten(raw)}

    @registry.tool(
        name="formcodec_to_json",
        description=(
            "把表单键值对转成 JSON 字符串，并按声明的类型做转换"
            "（可选传入字段类型描述）。当需要把表单数据以 JSON 形式"
            "交给后续流程、或展示给用户确认时使用。"
        ),
        properties={
            "pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
                "description": "键值对列表",
            },
            "casts": {
                "type": ["object", "null"],
                "default": None,
                "description": '字段类型映射，如 {"age": "int", "active": "bool", '
                '"tags": "list"}；未列出的字段保持字符串',
                "additionalProperties": {"type": "string"},
            },
        },
    )
    def formcodec_to_json(
        pairs: list[dict[str, Any]], casts: dict[str, str] | None = None
    ) -> dict[str, Any]:
        import json

        raw = [(p["key"], p.get("value")) for p in pairs]
        caster = {
            "int": int, "integer": int, "float": float, "number": float,
            "bool": bool, "boolean": bool, "str": str, "string": str,
        }
        # 先按字段名归并同名键，避免同一字段被写成多行导致值变成列表
        grouped: dict[str, list[Any]] = {}
        for k, v in raw:
            grouped.setdefault(k, []).append(v)

        out: dict[str, Any] = {}
        for k, values in grouped.items():
            target = str((casts or {}).get(k, "str"))
            if target == "list":
                # 合并多值后按逗号再拆一次，兼容 "a,b" 与 "a"&"b" 两种写法
                merged: list[str] = []
                for v in values:
                    if v is None:
                        continue
                    merged.extend(x for x in str(v).split(",") if x)
                out[k] = merged
            elif len(values) > 1:
                out[k] = [v for v in values]
            else:
                v = values[0]
                if target in caster and target != "str":
                    from .codec import decode_value

                    out[k] = decode_value("" if v is None else str(v),
                                          caster[target], path=k)
                else:
                    out[k] = "" if v is None else str(v)
        return {"json": json.dumps(out, ensure_ascii=False, indent=2)}

    @registry.tool(
        name="formcodec_parse_path",
        description=(
            "解析表单键路径语法：'.' 分隔嵌套字段，'[0]' 是数组索引，'[key]' 是"
            "map 的键。返回解析出的字段名、索引、map 键与嵌套深度。"
            "当需要理解或构造这类表单字段名（a.b[0].c、m[key]）时使用。"
        ),
        properties={
            "path": {
                "type": "string",
                "description": "待解析的键路径，如 Address[0].Name",
            },
        },
    )
    def formcodec_parse_path(path: str) -> dict[str, Any]:
        parsed = KeyPath.parse(path)
        return {
            "raw": parsed.raw,
            "root": parsed.root,
            "terminal": parsed.terminal,
            "indices": parsed.indices,
            "keys": parsed.keys,
            "depth": parsed.depth,
            "has_index": parsed.has_index,
            "fields": [t.name for t in parsed.tokens if t.kind == "field"],
        }

    @registry.tool(
        name="formcodec_validate_pairs",
        description=(
            "校验表单键值对是否符合预期：必填字段是否齐、取值是否在允许集合内、"
            "列表长度是否满足要求、是否有未声明的字段。"
            "返回每项检查的通过状态与具体问题。"
            "当需要在提交前自检表单数据、或解释「为什么这个请求被拒绝」时使用。"
        ),
        properties={
            "pairs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "required": ["key", "value"],
                    "additionalProperties": False,
                },
                "description": "待校验的键值对",
            },
            "required_fields": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "必填字段名列表",
            },
            "allowed_values": {
                "type": ["object", "null"],
                "default": None,
                "description": '字段允许取值，形状为 {"字段名": ["允许值", ...]}，'
                '如 {"status": ["active", "closed"]}',
                # 声明 additionalProperties 让 strict 模式知道值的形状；
                # dict 的键是变量名，无法列进 properties，只能这样约束
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "allow_unknown": {
                "type": ["boolean", "null"],
                "default": True,
                "description": "是否允许出现未声明字段",
            },
        },
    )
    def formcodec_validate_pairs(
        pairs: list[dict[str, Any]],
        required_fields: list[str] | None = None,
        allowed_values: dict[str, list[str]] | None = None,
        allow_unknown: bool | None = None,
    ) -> dict[str, Any]:
        keys = [p["key"] for p in pairs]
        present = set(keys)
        issues: list[dict[str, str]] = []

        for f in required_fields or []:
            if f not in present:
                issues.append({
                    "level": "error", "code": "missing_required",
                    "message": f"缺少必填字段 {f!r}",
                })

        for field, allowed in (allowed_values or {}).items():
            # 注意解包：每个 pair 是 {"key": ..., "value": ...}，
            # 必须按字段名取值，不能直接 `for k, v in pairs` 解包 dict——
            # 那样拿到的是 dict 的两个字符串键（"key"/"value"），永远匹配不上。
            for pair in pairs:
                if pair.get("key") == field and str(pair.get("value")) not in allowed:
                    issues.append({
                        "level": "error", "code": "invalid_value",
                        "message": f"{field}={pair.get('value')!r} 不在允许取值 {allowed} 内",
                    })

        if allow_unknown is False:
            known = set(required_fields or []) | set(allowed_values or {})
            for k in present - known:
                issues.append({
                    "level": "warning", "code": "unknown_field",
                    "message": f"出现未声明字段 {k!r}",
                })

        return {
            "valid": not any(i["level"] == "error" for i in issues),
            "field_count": len(present),
            "duplicate_keys": sorted({k for k in keys if keys.count(k) > 1}),
            "issues": issues,
        }
