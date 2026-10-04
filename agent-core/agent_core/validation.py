"""参数校验、业务校验与错误规整。

对应 OpenAI 文档里明确写出的执行链：
    arguments -> JSON 解析 -> schema 校验 -> 业务规则校验 -> 权限检查 -> 执行

以及一条重要的实践提醒：strict 模式只约束模型生成的参数形状，
不会替你检查订单是否存在、金额是否超限、服务是否可用。
所以这里额外提供 required 之外的业务级校验钩子。
"""

from __future__ import annotations

import json
from typing import Any, Callable

# 自定义校验异常：调用方据此区分「重试」还是「报错给用户」
class ValidationError(Exception):
    """参数不合法。重试没有意义，应直接把错误回传给 LLM 让它改参数。"""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class ExecutionError(Exception):
    """工具执行期错误。是否重试取决于 spec.retryable。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def parse_arguments(raw: Any) -> dict[str, Any]:
    """解析 LLM 返回的 arguments。

    注意 arguments 在 API 层通常是 **JSON 编码的字符串**，不是 dict——
    两种形态都要接住，否则线上会莫名其妙解析失败。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"arguments 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValidationError(
                f"arguments 必须是对象，收到 {type(parsed).__name__}"
            )
        return parsed
    raise ValidationError(f"无法解析的 arguments 类型：{type(raw).__name__}")


# ---------- 轻量 JSON Schema 校验（零依赖，覆盖 strict 模式的关键约束） ----------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_matches(value: Any, spec: Any) -> bool:
    if isinstance(spec, list):  # nullable 联合类型，如 ["string", "null"]
        return any(_type_matches(value, t) for t in spec)
    if spec == "integer":
        # Python 的 bool 是 int 的子类，需显式排除
        return isinstance(value, int) and not isinstance(value, bool)
    if spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _TYPES.get(spec)
    if expected is None:
        return True  # 未知类型不做拦截
    return isinstance(value, expected)


def validate_against_schema(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """按 schema 递归校验，不符合就抛 ValidationError。"""
    stype = schema.get("type")
    if stype and not _type_matches(value, stype):
        raise ValidationError(
            f"{path} 类型错误：期望 {stype}，实际 {type(value).__name__}",
            field=path,
        )

    if stype == "object" or (isinstance(value, dict) and "properties" in schema):
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if schema.get("additionalProperties") is False and isinstance(value, dict):
            unknown = set(value) - set(props)
            if unknown:
                raise ValidationError(
                    f"{path} 含未声明的字段 {sorted(unknown)}；"
                    f"允许的字段为 {sorted(props)}",
                    field=path,
                )
        for key in required:
            if isinstance(value, dict) and key not in value:
                raise ValidationError(f"{path} 缺少必填字段 {key!r}", field=f"{path}.{key}")
        for key, sub in props.items():
            if isinstance(value, dict) and key in value and value[key] is not None:
                validate_against_schema(value[key], sub, path=f"{path}.{key}")

    if stype == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                validate_against_schema(item, item_schema, path=f"{path}[{i}]")

    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(
            f"{path} 取值非法：{value!r} 不在允许集合 {schema['enum']} 内", field=path
        )


def validate_params(
    arguments: dict[str, Any],
    schema: dict[str, Any],
    *,
    extra_checks: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """完整校验：schema 校验 → 业务校验钩子。"""
    validate_against_schema(arguments, schema, path="args")
    if extra_checks is not None:
        extra_checks(arguments)


def coerce_defaults(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """按 schema 的 `default` 扩展填充缺失字段。

    这样工具实现内部可以直接安全取值，不用到处写 `args.get(k, default)`。
    """
    result = dict(arguments)
    for key, sub in (schema.get("properties") or {}).items():
        if key not in result and isinstance(sub, dict) and "default" in sub:
            result[key] = sub["default"]
    return result
