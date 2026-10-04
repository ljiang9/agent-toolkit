"""类型转换与编解码核心。

复刻自 go-playground/form 的 `Decoder` / `Encoder`，并把 Go 的 struct tag
机制替换为 Python 的类型注解 + 可选 `FieldSpec` 元数据。

**双向对等**是这个库的核心契约：`decode_form(encode_form(x)) == x`。
每个类型都必须同时实现 encode 与 decode，且对"空值"的处理保持一致。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Union, get_args, get_origin
from uuid import UUID

# 需要用类型往返（string -> python -> string）而非直接 str() 的类型
_TRUE_VALUES = {"true", "1", "yes", "on", "y", "t"}
_FALSE_VALUES = {"false", "0", "no", "off", "n", "f", ""}


class CodecError(ValueError):
    """编解码错误。消息里带上字段路径，便于定位。"""


# ---------- 单值类型转换 ----------

def to_bool(raw: str, *, path: str = "") -> bool:
    """字符串转布尔。

    比 `bool(str)` 严格得多：`bool("false")` 是 True 显然不符合预期。

    >>> to_bool("true"), to_bool("false"), to_bool("0")
    (True, False, False)
    """
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise CodecError(f"{path or '值'}：无法把 {raw!r} 解析为布尔值")


def from_bool(value: bool) -> str:
    """布尔转字符串，用小写 'true'/'false'（Go 与 JSON 惯例）。"""
    return "true" if value else "false"


def to_int(raw: str, *, path: str = "") -> int:
    value = raw.strip()
    if not value:
        raise CodecError(f"{path or '值'}：空字符串不能解析为整数")
    try:
        # 支持 "0x1f" 与 "0b101" 这类带前缀的写法
        if value.lower().startswith(("0x", "-0x")):
            return int(value, 16)
        if value.lower().startswith(("0b", "-0b")):
            return int(value, 2)
        if value.lower().startswith(("0o", "-0o")):
            return int(value, 8)
        return int(value)
    except ValueError as exc:
        raise CodecError(f"{path or '值'}：无法把 {raw!r} 解析为整数") from exc


def to_float(raw: str, *, path: str = "") -> float:
    value = raw.strip()
    if not value:
        raise CodecError(f"{path or '值'}：空字符串不能解析为浮点数")
    try:
        return float(value)
    except ValueError as exc:
        raise CodecError(f"{path or '值'}：无法把 {raw!r} 解析为浮点数") from exc


def to_datetime(raw: str, *, path: str = "", fmt: str | None = None) -> datetime:
    """字符串转时间。

    默认按 ISO 8601 / RFC 3339 解析（Go 的 time.RFC3339），
    这也是 HTML `datetime-local` 输入框的格式。
    """
    value = raw.strip()
    if fmt:
        try:
            return datetime.strptime(value, fmt)
        except ValueError as exc:
            raise CodecError(f"{path or '值'}：{raw!r} 不符合格式 {fmt!r}") from exc
    try:
        # fromisoformat 能处理 "2026-01-02T03:04:05" 与带时区的 "...+08:00"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    # 退化：只给日期的情况
    try:
        return datetime.combine(date.fromisoformat(value), time())
    except ValueError as exc:
        raise CodecError(
            f"{path or '值'}：{raw!r} 不是合法的 ISO 8601 时间"
        ) from exc


def from_datetime(value: datetime) -> str:
    """时间转字符串。用 isoformat 保证可逆。"""
    return value.isoformat()


def to_list(raw: str, *, path: str = "") -> list[str]:
    """把逗号分隔的字符串拆成列表。

    表单里没有真正的数组类型，惯例是逗号分隔或重复字段名。

    >>> to_list("a,b,c")
    ['a', 'b', 'c']
    >>> to_list("")
    []
    """
    value = raw.strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def to_json(raw: str, *, path: str = "") -> Any:
    """从 JSON 字符串解析任意对象。

    用于 dict/list 字段——表单里塞结构化数据时最实用的逃生舱。
    """
    value = raw.strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CodecError(f"{path or '值'}：不是合法 JSON（{exc.msg}）") from exc


def from_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------- 类型分派 ----------

def _unwrap_optional(tp: Any) -> Any:
    """剥掉 Optional[...] / Union[X, None]，返回 X 与是否可空。"""
    origin = get_origin(tp)
    if origin is Union or origin is getattr(__import__("types"), "UnionType", Union):
        args = [a for a in get_args(tp) if a is not type(None)]
        nullable = len(args) != len(get_args(tp))
        if len(args) == 1:
            return args[0], nullable
        return args[0], nullable  # 多类型联合由调用方另行处理
    return tp, False


def decode_value(raw: str, tp: Any, *, path: str = "") -> Any:
    """按目标类型把字符串解码成 Python 值。

    >>> decode_value("42", int)
    42
    >>> decode_value("true", bool)
    True
    >>> decode_value("a,b", list[str])
    ['a', 'b']
    """
    tp, _nullable = _unwrap_optional(tp)
    origin = get_origin(tp) or tp

    if origin is bool:
        return to_bool(raw, path=path)
    if origin is int:
        return to_int(raw, path=path)
    if origin is float:
        return to_float(raw, path=path)
    if origin is str:
        return raw
    if origin in (list, set, tuple):
        return decode_list(raw, tp, path=path)
    # 嵌套 dataclass：先解出 dict 再递归，而不是当 JSON 字符串处理。
    # 这一步必须在 dict 分支之前，否则嵌套结构体会被拍平成字符串。
    if dataclasses.is_dataclass(origin):
        from .core import decode_form  # 延迟导入避免循环依赖

        if raw is None or raw == "":
            # 可空字段没给值：返回 None 而不是去解一个空对象
            return None
        if isinstance(raw, Mapping):
            return decode_form(raw, origin)
        if isinstance(raw, str):
            parsed = to_json(raw, path=path)
            if parsed is None:
                return None
            return decode_form(parsed, origin)
        return decode_form({}, origin)
    if origin is dict:
        return to_json(raw, path=path)
    if origin is datetime:
        return to_datetime(raw, path=path)
    if origin is date:
        return to_datetime(raw, path=path).date()
    if origin is time:
        return to_datetime(raw, path=path).time()
    if origin is timedelta:
        return _parse_timedelta(raw, path=path)
    if origin is UUID:
        try:
            return UUID(raw.strip())
        except ValueError as exc:
            raise CodecError(f"{path or '值'}：{raw!r} 不是合法 UUID") from exc
    if origin in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        try:
            return origin(raw.strip())
        except ValueError as exc:
            raise CodecError(f"{path or '值'}：{raw!r} 不是合法 IP 地址") from exc
    if isinstance(origin, type) and issubclass(origin, Enum):
        try:
            return origin(raw.strip())
        except ValueError as exc:
            allowed = [e.value for e in origin]
            raise CodecError(
                f"{path or '值'}：{raw!r} 不是 {origin.__name__} 的合法取值，可选 {allowed}"
            ) from exc
    # 兜底：有 from_str 约定就调它，否则原样返回字符串
    if isinstance(origin, type) and hasattr(origin, "from_str"):
        return origin.from_str(raw)
    return raw


def _parse_timedelta(raw: str, *, path: str = "") -> timedelta:
    """解析时长，支持 "1h30m"、"90s"、"1.5h" 这类写法。"""
    value = raw.strip()
    match = re.fullmatch(
        r"(?:(?P<d>\d+(?:\.\d+)?)d)?"
        r"(?:(?P<h>\d+(?:\.\d+)?)h)?"
        r"(?:(?P<m>\d+(?:\.\d+)?)m)?"
        r"(?:(?P<s>\d+(?:\.\d+)?)s?)?",
        value,
    )
    if not match or not any(match.groupdict().values()):
        raise CodecError(f"{path or '值'}：{raw!r} 不是合法时长（如 1h30m、90s）")
    g = match.groupdict()
    return timedelta(
        days=float(g["d"] or 0),
        hours=float(g["h"] or 0),
        minutes=float(g["m"] or 0),
        seconds=float(g["s"] or 0),
    )


def _format_timedelta(delta: timedelta) -> str:
    """把时长格式化为 ``1h30m0s`` 形式，保证可逆。"""
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}h{m}m{s}s"


def decode_list(raw: str, tp: Any, *, path: str = "") -> list[Any]:
    """解码列表，按元素类型逐项转换。

    支持两种来源：逗号分隔字符串，或由多个同名键提供的值列表
    （`decode_form` 会把同名字段的多值合并后调用这里）。
    """
    args = get_args(tp)
    item_tp = args[0] if args else str
    items = raw if isinstance(raw, list) else to_list(raw, path=path)
    if item_tp is str:
        return items
    return [decode_value(_stringify(v), item_tp, path=f"{path}[{i}]")
            for i, v in enumerate(items)]


def _stringify(value: Any) -> str:
    """把任意 Python 值转成字符串，供 decode_value 消费。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return from_bool(value)
    if isinstance(value, datetime):
        return from_datetime(value)
    if isinstance(value, timedelta):
        return _format_timedelta(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple, dict)):
        return from_json(list(value) if isinstance(value, tuple) else value)
    return str(value)


def encode_value(value: Any, tp: Any = None) -> str:
    """把 Python 值编码成字符串。

    >>> encode_value(42), encode_value(True), encode_value(None)
    ('42', 'true', '')
    """
    if value is None:
        return ""
    return _stringify(value)


# ---------- 字段元数据 ----------

@dataclass
class FieldSpec:
    """字段级元数据（对应 Go 的 struct tag）。

    >>> spec = FieldSpec(default=10, options=["a", "b"])
    >>> spec.validate("a"), spec.validate("c")
    (True, False)
    """

    default: Any = None
    # 允许的取值；空元组表示不限制
    options: tuple[str, ...] = ()
    # 逗号分隔列表的项数下限/上限
    min_items: int | None = None
    max_items: int | None = None
    # 是否从输入中排除（常用于确认框）
    ignore: bool = False
    # 自定义标签名，优先于类型注解的字段名
    alias: str | None = None

    def validate(self, value: str) -> bool:
        """值是否合法。"""
        if self.options and value not in self.options:
            return False
        return True

    def check_size(self, count: int) -> bool:
        """列表长度是否合法。"""
        if self.min_items is not None and count < self.min_items:
            return False
        if self.max_items is not None and count > self.max_items:
            return False
        return True
