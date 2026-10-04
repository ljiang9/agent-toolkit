"""双向编解码：结构体/字典 ⇄ 键值对集合。

复刻自 go-playground/form 的核心 API：
    decode_form(values, Target)   把键值对解成结构体
    encode_form(obj)              把结构体编码成键值对

两者的对称性是可测试的：`decode_form(encode_form(x), type(x)) == x`。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, TypeVar, get_type_hints

from .codec import (
    CodecError,
    FieldSpec,
    _stringify,
    decode_list,
    decode_value,
    encode_value,
    to_list,
)
from .path import KeyPath, PathError, PathToken, encode_path

T = TypeVar("T")

# 表单里数组的两种表示：重复字段名（tags=a&tags=b）或索引（tags[0]=a）
_APPEND = "append"


def _is_dataclass_type(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _field_spec(field_info: dataclasses.Field) -> FieldSpec:
    """从 dataclass field 的 metadata 里读出 FieldSpec。"""
    meta = field_info.metadata.get("form")
    if isinstance(meta, FieldSpec):
        return meta
    if isinstance(meta, dict):
        return FieldSpec(**meta)
    return FieldSpec()


def _type_hints(tp: Any) -> dict[str, Any]:
    """取类型注解，解析失败时退回原始 __annotations__。

    `from __future__ import annotations` 会把注解变成字符串，
    get_type_hints 负责求值；但若类型定义在局部作用域里可能失败，
    此时不能崩——编码不依赖类型注解，退回原样即可。
    """
    try:
        return get_type_hints(tp)
    except Exception:  # noqa: BLE001
        return dict(getattr(tp, "__annotations__", {}))


# ---------- 解码 ----------


def decode_form(
    values: Mapping[str, Any] | list[tuple[str, Any]],
    target: type[T],
    *,
    strict: bool = False,
) -> T:
    """把键值对解码成 target 指定的对象。

    参数
    ----
    values:
        既接受 dict（多值字段用 list 表示），也接受 [(key, value)] 对列表
        —— 后者能保留同名键的原始顺序与重复。
    target:
        dataclass 类型、TypedDict 或普通 dict。
    strict:
        为 True 时，遇到 target 里没有的键会报错；
        为 False（默认）时静默忽略——表单里常有 submit 按钮之类的杂项。

    >>> from dataclasses import dataclass
    >>> @dataclass
    ... class U:
    ...     name: str
    ...     age: int
    >>> decode_form({"name": "joey", "age": "3"}, U)
    U(name='joey', age=3)
    """
    pairs = _normalize(values)
    grouped = _group(_flatten(pairs))

    if target is dict or target is Any:
        # dict 目标：重复键合并成列表，单值键保持标量——
        # 直接返回 _group 的结果会让所有值都变成列表，不符合直觉。
        return _collapse_scalars(_group(_flatten(pairs)))  # type: ignore[return-value]
    if _is_dataclass_type(target):
        return _decode_dataclass(grouped, target, strict=strict)  # type: ignore[return-value]
    raise CodecError(f"不支持的目标类型 {target!r}；请用 dataclass 或 dict")


def _normalize(values: Mapping[str, Any] | list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """统一成 [(key, value)] 列表，并把多值字段拆成重复键。"""
    if isinstance(values, Mapping):
        out: list[tuple[str, Any]] = []
        for k, v in values.items():
            if isinstance(v, (list, tuple)):
                out.extend((k, item) for item in v)
            else:
                out.append((k, v))
        return out
    return list(values)


def _flatten(pairs: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """规范化键值对，保留重复键。

    **不做整体反扁平化**——那会毁掉同名重复键（数组的常规表示）。
    嵌套结构由 `_decode_dataclass` 按字段局部处理：
    某个字段下面出现 `name[0].sub` 时才把该字段还原成嵌套结构。
    """
    return [(k, _stringify(v)) for k, v in pairs]


def unflatten(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把扁平的表单键还原成嵌套字典。

    这是表单编码的逆操作：`parent.child` 与 `parent[0].child` 是表单里
    表达嵌套结构的两种标准写法，这里都支持。

    >>> unflatten([("a.b", "1"), ("a.c", "2")])
    {'a': {'b': '1', 'c': '2'}}
    >>> unflatten([("t[0]", "a"), ("t[1]", "b")])
    {'t': ['a', 'b']}
    >>> unflatten([("m[k]", "v")])
    {'m': {'k': 'v'}}
    """
    out: dict[str, Any] = {}
    for key, value in pairs:
        try:
            parsed = KeyPath.parse(key)
        except Exception:  # noqa: BLE001 - 键路径非法就当平铺键处理
            out[key] = value
            continue
        _place(out, parsed.tokens, value)
    return _collapse_lists(out)


def _place(cursor: dict[str, Any], tokens: list[PathToken], value: Any) -> None:
    """按 token 序列把值写入嵌套结构。

    逐层下降：field 进字典、index 进列表、key 进子字典。
    只有在最后一层才写入实际值，中间层只负责建容器。
    """
    token = tokens[0]
    rest = tokens[1:]

    if token.kind == "key":
        # map 键：`m[k]` 形状里，key 前必须有 field 名（KeyPath 保证）
        # 这里的 token 是 field 之后的第一个 token 时，cursor 就是该 map
        cursor[token.key] = value
        return

    if token.kind == "field":
        if not rest:
            cursor[token.name] = value
            return
        nxt = rest[0]
        if nxt.kind == "index":
            bucket = cursor.setdefault(token.name, [])
            if not isinstance(bucket, list):
                bucket = []
                cursor[token.name] = bucket
            _fill_index(bucket, rest, value)
            return
        if nxt.kind == "key":
            sub = cursor.setdefault(token.name, {})
            if not isinstance(sub, dict):
                sub = {}
                cursor[token.name] = sub
            _place(sub, rest, value)
            return
        # 连续的 field
        sub = cursor.get(token.name)
        if not isinstance(sub, dict):
            sub = {}
            cursor[token.name] = sub
        _place(sub, rest, value)
        return

    if token.kind == "index":
        # 以 [ 开头：根节点本身是数组
        bucket = cursor.setdefault("__root_list__", [])
        _fill_index(bucket, tokens, value)
        return

    # append token：追加到列表末尾
    return


def _fill_index(bucket: list[Any], tokens: list[PathToken], value: Any) -> None:
    """把 tokens 中的 index/append 序列落到列表里。"""
    i = 0
    n = len(tokens)
    while i < n and tokens[i].kind in ("index", "append"):
        token = tokens[i]
        idx = token.index if token.kind == "index" else len(bucket)
        while len(bucket) <= idx:
            bucket.append({})
        i += 1

    if i >= n:
        # 没有更深层：直接把值放进槽位
        token = tokens[-1]
        idx = token.index if token.kind == "index" else len(bucket) - 1
        bucket[idx] = value
        return

    # 数组元素本身是 dict（[0].field）
    token = tokens[i - 1]
    idx = token.index if token.kind == "index" else len(bucket) - 1
    slot = bucket[idx]
    if not isinstance(slot, dict):
        slot = {}
        bucket[idx] = slot
    _place(slot, tokens[i:], value)


def _collapse_lists(obj: Any) -> Any:
    """把内部的 __root_list__ / 字典化列表还原成正常结构。"""
    if isinstance(obj, dict):
        if set(obj) == {"__root_list__"}:
            return _collapse_lists(obj["__root_list__"])
        return {k: _collapse_lists(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_collapse_lists(v) for v in obj]
    return obj


def _collapse_scalars(grouped: Mapping[str, list[Any]]) -> dict[str, Any]:
    """单值键取标量，多值键保留列表。

    >>> _collapse_scalars({"a": ["1"], "b": ["x", "y"]})
    {'a': '1', 'b': ['x', 'y']}
    """
    out: dict[str, Any] = {}
    for key, values in grouped.items():
        out[key] = values[0] if len(values) == 1 else list(values)
    return out


def _group(pairs: list[tuple[str, Any]]) -> dict[str, list[Any]]:
    """按键聚合，保留同名字段的所有值与原始顺序。

    表单里数组的表示就是重复键（`tags=a&tags=b`），因此同名值必须全部
    保留——只取最后一个会静默丢数据。
    """
    grouped: dict[str, list[Any]] = {}
    for k, v in pairs:
        grouped.setdefault(k, []).append(v)
    return grouped


def _collect_indexed(grouped: Mapping[str, list[Any]], name: str) -> dict[int, Any]:
    """把 `name[0]` / `name[1]` 形式的键收集成 {索引: 值}。"""
    prefix = name + "["
    out: dict[int, Any] = {}
    for key, values in grouped.items():
        if not key.startswith(prefix):
            continue
        close = key.find("]")
        if close == -1:
            continue
        idx_text = key[len(prefix) : close]
        if not idx_text.lstrip("-").isdigit():
            continue
        out[int(idx_text)] = values[0] if values else ""
    return out


def _collect_dotted(grouped: Mapping[str, list[Any]], name: str) -> dict[int, dict[str, Any]]:
    """把 `name[0].sub` 与 `name.sub` 收集成 {索引: {子字段: 值}}。

    嵌套 dataclass 在表单里就是 `Address[0].Name` 这种扁平写法，
    这里按索引分组还原。

    >>> _collect_dotted({"a[0].b": ["1"], "a[1].b": ["2"]}, "a")
    {0: {'b': '1'}, 1: {'b': '2'}}
    >>> _collect_dotted({"a.b": ["1"]}, "a")
    {0: {'b': '1'}}
    """
    out: dict[int, dict[str, Any]] = {}
    bracketed = name + "["
    dotted = name + "."
    for key, values in grouped.items():
        if not values:
            continue
        value = values[0] if len(values) == 1 else values
        if key.startswith(bracketed):
            close = key.find("]")
            if close == -1:
                continue
            idx_text = key[len(bracketed) : close]
            field = key[close + 1 :]
            if not idx_text.lstrip("-").isdigit() or not field.startswith("."):
                continue
            idx = int(idx_text)
            out.setdefault(idx, {})[field[1:]] = value
        elif key.startswith(dotted):
            # 无索引的嵌套：整体当作第 0 项
            out.setdefault(0, {})[key[len(dotted) :]] = value
    return out


def _has_indexed(grouped: Mapping[str, Any], name: str) -> bool:
    """是否存在 `name[...]` 形式的键。"""
    prefix = name + "["
    return any(k.startswith(prefix) for k in grouped)


def _decode_dataclass(
    grouped: Mapping[str, list[Any]],
    target: type[T],
    *,
    strict: bool,
) -> T:
    """按 dataclass 字段定义解码。"""
    hints = _type_hints(target)
    # 只取 dataclass 自身声明的字段，继承来的按名字匹配即可
    fields = {f.name: f for f in dataclasses.fields(target)}
    # form_spec 是本扩展：类级字段元数据
    form_spec: dict[str, FieldSpec] = getattr(target, "__form_spec__", {})

    kwargs: dict[str, Any] = {}
    consumed: set[str] = set()

    for name, field_info in fields.items():
        spec = form_spec.get(name) or _field_spec(field_info)
        if spec.ignore:
            continue
        key = spec.alias or name
        tp = hints.get(name, field_info.type)

        # 嵌套形式 `name.field` / `name[0].field`：还原成 dict 交给类型转换
        dotted = _collect_dotted(grouped, key)
        indexed = _collect_indexed(grouped, key)
        if dotted or indexed:
            consumed.update(
                k for k in grouped
                if k.startswith(key + ".") or k.startswith(key + "[")
            )
            origin = getattr(tp, "__origin__", tp)
            wants_list = origin in (list, tuple, set)
            if indexed or wants_list:
                # 有显式索引，或目标类型就是列表 -> 还原成有序列表
                items = []
                for i in sorted(set(dotted) | set(indexed)):
                    merged = dict(dotted.get(i) or {})
                    # 同一索引下既有子字段又有裸值（如 a[0]=x 且 a[0].b=y）时，
                    # 裸值放在 "_value" 键下，避免类型冲突
                    if i in indexed:
                        merged.setdefault("_value", indexed[i])
                    items.append(merged)
                kwargs[name] = _coerce(items, tp, key, spec)
            else:
                # 只有 name.sub 形式且目标是单个对象 -> 还原成一个 dict
                merged = {}
                for sub in dotted.values():
                    merged.update(sub)
                if key in grouped:
                    merged.setdefault("_value", grouped[key][0])
                kwargs[name] = _coerce(merged, tp, key, spec)
            continue

        consumed.add(key)
        raw = _resolve_value(key, grouped, spec)
        if raw is _MISSING:
            if spec.default is not None or _has_default(field_info):
                kwargs[name] = _default_for(field_info, spec)
                continue
            # 缺必填字段时用类型零值，避免整个解码失败——
            # 与原项目「Only creates objects as necessary」的宽容取向一致
            kwargs[name] = _zero_value(tp)
            continue
        kwargs[name] = _coerce(raw, tp, key, spec)

    if strict:
        extra = set(grouped) - consumed
        if extra:
            raise CodecError(f"发现未定义的字段：{sorted(extra)}")

    return target(**kwargs)  # type: ignore[call-arg]


class _Missing:
    """哨兵：区分「字段缺失」与「字段值为 None」。"""

    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


_MISSING = _Missing()


def _resolve_value(key: str, grouped: Mapping[str, list[Any]],
                   spec: FieldSpec) -> Any:
    """解析字段的原始输入，失败返回 _MISSING。"""
    values = grouped.get(key)
    if values is None:
        if spec.alias:
            values = grouped.get(spec.alias)
        if values is None:
            return _MISSING
    # 单值字段取第一个，多值字段保留全部（重复键是数组的常规表示）
    if len(values) == 1:
        return values[0]
    return values


def _as_mapping(item: Any, grouped: Mapping[str, list[Any]],
                key: str, index: int) -> Any:
    """把 `name[i]` 还原成 dict——递归收集 `name[i].sub` 形式的键。

    >>> _as_mapping("", {"a[0].b": ["1"]}, "a", 0)
    {'b': '1'}
    """
    if isinstance(item, Mapping):
        return dict(item)
    prefix = f"{key}[{index}]."
    out: dict[str, Any] = {}
    for k, values in grouped.items():
        if not k.startswith(prefix):
            continue
        field = k[len(prefix) :]
        if not values:
            continue
        # 同名字段的多值合并成列表（如 a[0].tag=x&a[0].tag=y）
        out[field] = values[0] if len(values) == 1 else values
    return out


def _has_default(field_info: dataclasses.Field) -> bool:
    return (
        field_info.default is not dataclasses.MISSING
        or field_info.default_factory is not dataclasses.MISSING  # type: ignore[misc]
    )


def _default_for(field_info: dataclasses.Field, spec: FieldSpec) -> Any:
    if spec.default is not None:
        return spec.default
    if field_info.default is not dataclasses.MISSING:
        return field_info.default
    if field_info.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return field_info.default_factory()  # type: ignore[misc]
    return None


def _zero_value(tp: Any) -> Any:
    """类型零值：缺失的 int 为 0、str 为 ''、bool 为 False。"""
    origin = getattr(tp, "__origin__", tp)
    mapping = {int: 0, float: 0.0, str: "", bool: False, list: list, dict: dict}
    for key, val in mapping.items():
        if origin is key:
            return val() if callable(val) else val
    return None


def _coerce(raw: Any, tp: Any, key: str, spec: FieldSpec) -> Any:
    """按类型转换原始输入。"""
    if isinstance(raw, str) and spec.options and raw not in spec.options:
        raise CodecError(
            f"{key}：{raw!r} 不在允许取值 {list(spec.options)} 内"
        )

    # list 目标：多值输入优先，否则按逗号分隔解析
    origin = getattr(tp, "__origin__", tp)
    if origin in (list, set, tuple):
        if isinstance(raw, list):
            items: list[Any] = list(raw)
        else:
            # 单个字符串按逗号分隔拆开——表单里数组的两种表示都要认
            items = to_list(str(raw)) if isinstance(raw, str) else [raw]
        decoded = decode_list(items, tp, path=key)
        if not spec.check_size(len(decoded)):
            raise CodecError(
                f"{key}：期望 {spec.min_items}~{spec.max_items} 项，"
                f"实际 {len(decoded)} 项"
            )
        if origin is set:
            return set(decoded)
        if origin is tuple:
            return tuple(decoded)
        return decoded

    if isinstance(raw, list):
        # 单值字段收到多个值：取第一个，其余丢弃（与原项目行为一致）
        raw = raw[0] if raw else ""

    try:
        return decode_value(_stringify(raw), tp, path=key)
    except CodecError as exc:
        raise CodecError(str(exc)) from exc


# ---------- 编码 ----------


def encode_form(obj: Any, *, prefix: str = "") -> list[tuple[str, str]]:
    """把对象编码成 [(键, 值)] 列表。

    返回列表而非 dict，是为了保留同名重复键（表单里数组的常见表示）。

    >>> from dataclasses import dataclass
    >>> @dataclass
    ... class U:
    ...     name: str
    ...     tags: list[str]
    >>> encode_form(U("joey", ["a", "b"]))
    [('name', 'joey'), ('tags', 'a'), ('tags', 'b')]
    """
    out: list[tuple[str, str]] = []

    if obj is None:
        return out

    if isinstance(obj, Mapping):
        items = obj.items()
        for k, v in items:
            out.extend(_encode_entry(str(k), v, prefix))
        return out

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        hints = _type_hints(type(obj))
        form_spec: dict[str, FieldSpec] = getattr(type(obj), "__form_spec__", {})
        for f in dataclasses.fields(obj):
            spec = form_spec.get(f.name) or _field_spec(f)
            if spec.ignore:
                continue
            key = spec.alias or f.name
            value = getattr(obj, f.name)
            out.extend(_encode_entry(key, value, prefix, type_hint=hints.get(f.name)))
        return out

    raise CodecError(f"不支持的编码类型 {type(obj)!r}；请用 dataclass 或 dict")


def _encode_entry(
    key: str,
    value: Any,
    prefix: str = "",
    *,
    type_hint: Any = None,
) -> list[tuple[str, str]]:
    """编码单个字段，正确处理列表与嵌套。

    嵌套 dataclass 展开成 `parent.child` 形式的多个键（对齐 Go 与
    常见表单编码），而不是拍平成一段 JSON 字符串。
    """
    full_key = f"{prefix}.{key}" if prefix else key

    # 嵌套 dataclass -> 展开为 parent.child 形式
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: list[tuple[str, str]] = []
        hints = _type_hints(type(value))
        for f in dataclasses.fields(value):
            fspec: FieldSpec = getattr(type(value), "__form_spec__", {}).get(f.name) \
                or _field_spec(f)
            if fspec.ignore:
                continue
            out.extend(_encode_entry(fspec.alias or f.name, getattr(value, f.name),
                                     full_key, type_hint=hints.get(f.name)))
        return out

    # 列表 -> 重复键（最通用的表单表示）
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        # 列表里若是 dataclass，逐项展开成 key[0].field 形式
        if items and dataclasses.is_dataclass(items[0]) and not isinstance(items[0], type):
            out = []
            for idx, item in enumerate(items):
                for f in dataclasses.fields(item):
                    out.extend(
                        _encode_entry(f.name, getattr(item, f.name), f"{full_key}[{idx}]")
                    )
            return out
        return [(full_key, encode_value(v)) for v in items]

    # 字典 -> a[b] 形式（与索引表示兼容）
    if isinstance(value, Mapping):
        out = []
        for k, v in value.items():
            out.append((f"{full_key}[{k}]", encode_value(v)))
        return out

    return [(full_key, encode_value(value, type_hint))]


def decode_form_safe(
    values: Mapping[str, Any] | list[tuple[str, Any]],
    target: type[T],
) -> tuple[T | None, list[str]]:
    """容错版解码：返回 (结果, 错误列表) 而非抛异常。

    agent 场景常用——部分字段解码失败时仍能拿到其余字段，
    并把错误汇总给模型让它自己纠正。
    """
    errors: list[str] = []
    pairs = _normalize(values)

    try:
        return decode_form(pairs, target), errors
    except CodecError as exc:
        errors.append(str(exc))
        return None, errors


def form_to_query_string(pairs: list[tuple[str, str]]) -> str:
    """把键值对列表拼成 URL query 串（保留重复键）。"""
    from urllib.parse import quote

    return "&".join(f"{quote(k, safe='[]')}={quote(v, safe='')}" for k, v in pairs)


def query_string_to_form(query: str) -> list[tuple[str, str]]:
    """解析 URL query 串成键值对列表。"""
    from urllib.parse import parse_qsl

    return parse_qsl(query, keep_blank_values=True)


def form_to_json(values: Mapping[str, Any] | list[tuple[str, Any]],
                 target: type[T] | None = None) -> str:
    """一步把表单数据转成 JSON 字符串——agent 最常用的出口。"""
    import json

    obj = decode_form(values, target) if target is not None else _group(_normalize(values))
    return json.dumps(_to_jsonable(obj), ensure_ascii=False, indent=2)


def _to_jsonable(obj: Any) -> Any:
    """把解码结果转成 JSON 可序列化的形式。

    解码出的类型里 datetime/UUID/IP 等不是 JSON 原生类型，
    统一转成字符串——这正是 URL 表单里本来的形态。
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # datetime / date / time / UUID / IP / timedelta 等
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "total_seconds"):  # timedelta
        return obj.total_seconds()
    if isinstance(obj, Enum):
        return obj.value
    return str(obj)
