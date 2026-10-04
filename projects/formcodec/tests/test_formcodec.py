"""formcodec 测试。

最核心的契约是**往返一致性**：`decode_form(encode_form(x)) == x`。
这条性质一旦破坏，整个库就没用了，所以测试里反复验证它。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from ipaddress import IPv4Address
from uuid import UUID

import pytest

from formcodec import (
    CodecError,
    FieldSpec,
    KeyPath,
    PathError,
    decode_form,
    decode_form_safe,
    decode_value,
    encode_form,
    encode_path,
    encode_value,
    form_to_query_string,
    query_string_to_form,
    to_bool,
    to_datetime,
    to_float,
    to_int,
    to_list,
    unflatten,
)


# ---------- 测试用类型 ----------

class Color(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Address:
    name: str
    phone: str


@dataclass
class User:
    name: str
    age: int
    active: bool
    tags: list[str] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)
    home: Address | None = None


# ---------- 键路径解析 ----------

def test_keypath_simple() -> None:
    p = KeyPath.parse("name")
    assert p.root == "name"
    assert p.terminal == "name"
    assert p.depth == 1


def test_keypath_index() -> None:
    """对齐 go-playground/form 的 array[0] 语法。"""
    p = KeyPath.parse("Address[0].Name")
    assert p.root == "Address"
    assert p.terminal == "Name"
    assert p.indices == [0]
    assert p.has_index is True
    assert p.depth == 2


def test_keypath_map_key() -> None:
    p = KeyPath.parse("MapExample[key]")
    assert p.root == "MapExample"
    assert p.keys == ["key"]
    assert p.has_index is False


def test_keypath_nested_map() -> None:
    p = KeyPath.parse("NestedMap[k1][k2]")
    assert p.keys == ["k1", "k2"]


def test_keypath_nested_array() -> None:
    p = KeyPath.parse("NestedArray[0][1]")
    assert p.indices == [0, 1]


def test_keypath_multiple_indices() -> None:
    p = KeyPath.parse("a[2].b[3]")
    assert p.indices == [2, 3]


def test_keypath_empty_raises() -> None:
    with pytest.raises(PathError, match="不能为空"):
        KeyPath.parse("")


def test_keypath_unclosed_bracket_raises() -> None:
    with pytest.raises(PathError, match="配对的"):
        KeyPath.parse("a[0")


def test_keypath_trailing_dot_raises() -> None:
    with pytest.raises(PathError, match="以 '.' 结尾"):
        KeyPath.parse("a.")


def test_keypath_leading_bracket_raises() -> None:
    with pytest.raises(PathError, match="意外的"):
        KeyPath.parse("[0]")


def test_encode_path_roundtrip() -> None:
    assert encode_path("Address", 0, "Name") == "Address[0].Name"
    assert encode_path("MapExample", "key") == "MapExample[key]"


# ---------- 单值类型转换 ----------

def test_to_bool_variants() -> None:
    for t in ("true", "TRUE", "1", "yes", "on"):
        assert to_bool(t) is True
    for f in ("false", "0", "no", "off", ""):
        assert to_bool(f) is False


def test_to_bool_rejects_garbage() -> None:
    with pytest.raises(CodecError, match="布尔"):
        to_bool("maybe")


def test_to_bool_not_like_python_bool() -> None:
    """`bool(\"false\")` 是 True，必须显式处理而不是直接用 bool()。"""
    assert to_bool("false") is False


def test_to_int_with_prefixes() -> None:
    assert to_int("42") == 42
    assert to_int("0x1f") == 31
    assert to_int("0b101") == 5
    assert to_int("-7") == -7


def test_to_int_errors() -> None:
    with pytest.raises(CodecError, match="整数"):
        to_int("abc")
    with pytest.raises(CodecError, match="整数"):
        to_int("")


def test_to_float_errors() -> None:
    with pytest.raises(CodecError, match="浮点"):
        to_float("abc")


def test_to_datetime_iso() -> None:
    got = to_datetime("2026-01-02T03:04:05")
    assert got == datetime(2026, 1, 2, 3, 4, 5)


def test_to_datetime_with_timezone() -> None:
    got = to_datetime("2026-01-02T03:04:05+08:00")
    assert got.utcoffset() is not None


def test_to_datetime_date_only() -> None:
    assert to_datetime("2026-01-02") == datetime(2026, 1, 2, 0, 0)


def test_to_datetime_errors() -> None:
    with pytest.raises(CodecError, match="ISO 8601"):
        to_datetime("not-a-date")


def test_to_list_splits_and_strips() -> None:
    assert to_list("a, b ,c") == ["a", "b", "c"]
    assert to_list("") == []


def test_decode_value_by_type() -> None:
    assert decode_value("42", int) == 42
    assert decode_value("1.5", float) == 1.5
    assert decode_value("true", bool) is True
    assert decode_value("x", str) == "x"
    assert decode_value("a,b", list[str]) == ["a", "b"]


def test_decode_value_optional() -> None:
    assert decode_value("42", int | None) == 42


def test_decode_value_enum() -> None:
    assert decode_value("red", Color) is Color.RED
    with pytest.raises(CodecError, match="合法取值"):
        decode_value("green", Color)


def test_decode_value_uuid_and_ip() -> None:
    u = decode_value("123e4567-e89b-12d3-a456-426614174000", UUID)
    assert isinstance(u, UUID)
    ip = decode_value("192.168.1.1", IPv4Address)
    assert str(ip) == "192.168.1.1"


def test_decode_value_timedelta() -> None:
    assert decode_value("1h30m", timedelta) == timedelta(hours=1, minutes=30)
    assert decode_value("90s", timedelta) == timedelta(seconds=90)


def test_decode_value_date() -> None:
    assert decode_value("2026-01-02", date) == date(2026, 1, 2)


def test_encode_value_bool_uses_lowercase() -> None:
    assert encode_value(True) == "true"
    assert encode_value(False) == "false"
    assert encode_value(None) == ""


def test_encode_value_datetime_isoformat() -> None:
    assert encode_value(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"


# ---------- 解码基础 ----------

def test_decode_form_basic_types() -> None:
    u = decode_form({"name": "joey", "age": "3", "active": "true"}, User)
    assert u.name == "joey"
    assert u.age == 3 and isinstance(u.age, int)
    assert u.active is True


def test_decode_form_from_pairs() -> None:
    """表单输入天然是 (key, value) 对列表，应直接支持。"""
    u = decode_form([("name", "joey"), ("age", "3"), ("active", "true")], User)
    assert u.age == 3


def test_decode_form_repeated_keys_become_list() -> None:
    """tags=a&tags=b 是数组的常规表示，两个值都要保留。"""
    u = decode_form(
        [("name", "x"), ("age", "1"), ("active", "true"), ("tags", "a"), ("tags", "b")],
        User,
    )
    assert u.tags == ["a", "b"]


def test_decode_form_comma_separated_list() -> None:
    u = decode_form(
        {"name": "x", "age": "1", "active": "true", "tags": "a,b,c"}, User
    )
    assert u.tags == ["a", "b", "c"]


def test_decode_form_missing_fields_use_zero_value() -> None:
    """缺必填字段不应让整个解码崩溃——与原项目的宽容取向一致。"""
    u = decode_form({}, User)
    assert u.name == ""
    assert u.age == 0
    assert u.active is False
    assert u.tags == []


def test_decode_form_single_value_field_takes_first() -> None:
    """单值字段收到多值时取第一个，其余丢弃。"""
    u = decode_form(
        [("name", "x"), ("name", "y"), ("age", "1"), ("active", "true")], User
    )
    assert u.name == "x"


def test_decode_form_to_dict() -> None:
    out = decode_form({"a": "1"}, dict)
    assert out == {"a": "1"}


def test_decode_form_uses_field_default() -> None:
    @dataclass
    class T:
        limit: int = 10

    assert decode_form({}, T).limit == 10


def test_decode_form_uses_form_spec_default() -> None:
    @dataclass
    class T:
        limit: int = 0
        __form_spec__ = {"limit": FieldSpec(default=99)}

    assert decode_form({}, T).limit == 99


def test_decode_form_ignores_ignored_field() -> None:
    @dataclass
    class T:
        name: str
        _csrf: str = ""
        __form_spec__ = {"_csrf": FieldSpec(ignore=True)}

    out = decode_form({"name": "x", "_csrf": "tok"}, T)
    assert out.name == "x"
    assert out._csrf == ""


def test_decode_form_alias() -> None:
    @dataclass
    class T:
        user_name: str
        __form_spec__ = {"user_name": FieldSpec(alias="username")}

    assert decode_form({"username": "joey"}, T).user_name == "joey"


def test_decode_form_strict_rejects_unknown() -> None:
    @dataclass
    class T:
        a: str

    with pytest.raises(CodecError, match="未定义的字段"):
        decode_form({"a": "1", "extra": "2"}, T, strict=True)


def test_decode_form_lenient_ignores_unknown() -> None:
    @dataclass
    class T:
        a: str

    assert decode_form({"a": "1", "submit": ""}, T).a == "1"


def test_decode_form_validates_options() -> None:
    @dataclass
    class T:
        status: str
        __form_spec__ = {"status": FieldSpec(options=("active", "closed"))}

    assert decode_form({"status": "active"}, T).status == "active"
    with pytest.raises(CodecError, match="不在允许取值"):
        decode_form({"status": "other"}, T)


def test_decode_form_validates_list_size() -> None:
    @dataclass
    class T:
        tags: list[str] = field(default_factory=list)
        __form_spec__ = {"tags": FieldSpec(min_items=2, max_items=3)}

    assert decode_form([("tags", "a"), ("tags", "b")], T).tags == ["a", "b"]
    with pytest.raises(CodecError, match="期望"):
        decode_form([("tags", "a")], T)


def test_decode_form_bad_type_raises() -> None:
    with pytest.raises(CodecError, match="整数"):
        decode_form({"name": "x", "age": "abc", "active": "true"}, User)


def test_decode_form_nested_dataclass_from_json_string() -> None:
    u = decode_form(
        {"name": "x", "age": "1", "active": "true",
         "home": '{"name": "h", "phone": "1"}'},
        User,
    )
    assert u.home == Address("h", "1")


def test_decode_form_safe_reports_errors() -> None:
    obj, errors = decode_form_safe({"name": "x", "age": "bad", "active": "t"}, User)
    assert obj is None
    assert errors


def test_decode_form_safe_success() -> None:
    obj, errors = decode_form_safe(
        {"name": "x", "age": "1", "active": "true"}, User
    )
    assert errors == []
    assert obj is not None and obj.age == 1


# ---------- 嵌套还原 ----------

def test_unflatten_dotted() -> None:
    assert unflatten([("a.b", "1"), ("a.c", "2")]) == {"a": {"b": "1", "c": "2"}}


def test_unflatten_indexed() -> None:
    assert unflatten([("t[0]", "a"), ("t[1]", "b")]) == {"t": ["a", "b"]}


def test_unflatten_map() -> None:
    assert unflatten([("m[k]", "v"), ("m[j]", "w")]) == {"m": {"k": "v", "j": "w"}}


def test_unflatten_list_of_objects() -> None:
    got = unflatten([
        ("Address[0].Name", "x"), ("Address[0].Phone", "y"),
        ("Address[1].Name", "z"),
    ])
    assert got == {"Address": [{"Name": "x", "Phone": "y"}, {"Name": "z"}]}


def test_unflatten_illegal_key_kept_flat() -> None:
    assert unflatten([("a[", "1")]) == {"a[": "1"}


def test_decode_form_indexed_list_of_dataclass() -> None:
    u = decode_form([
        ("name", "joey"), ("age", "3"), ("active", "true"),
        ("addresses[0].name", "x"), ("addresses[0].phone", "1"),
        ("addresses[1].name", "y"), ("addresses[1].phone", "2"),
    ], User)
    assert u.addresses == [Address("x", "1"), Address("y", "2")]


def test_decode_form_dotted_single_object() -> None:
    u = decode_form([
        ("name", "joey"), ("age", "3"), ("active", "true"),
        ("home.name", "h"), ("home.phone", "1"),
    ], User)
    assert u.home == Address("h", "1")


# ---------- 编码 ----------

def test_encode_form_flat() -> None:
    pairs = encode_form({"name": "joey", "age": 3})
    assert dict(pairs) == {"name": "joey", "age": "3"}


def test_encode_form_list_uses_repeated_keys() -> None:
    assert encode_form({"tags": ["a", "b"]}) == [("tags", "a"), ("tags", "b")]


def test_encode_form_dict_uses_bracket() -> None:
    assert encode_form({"m": {"k": "v"}}) == [("m[k]", "v")]


def test_encode_form_nested_dataclass_uses_dot() -> None:
    pairs = encode_form({"home": Address("h", "1")})
    assert pairs == [("home.name", "h"), ("home.phone", "1")]


def test_encode_form_list_of_dataclass_uses_index() -> None:
    pairs = encode_form({"addresses": [Address("x", "1"), Address("y", "2")]})
    assert pairs == [
        ("addresses[0].name", "x"), ("addresses[0].phone", "1"),
        ("addresses[1].name", "y"), ("addresses[1].phone", "2"),
    ]


def test_encode_form_none_fields_skipped() -> None:
    """None 编码为空字符串而不是字面量 'None'。"""
    assert dict(encode_form({"a": None})) == {"a": ""}


def test_encode_form_dataclass() -> None:
    u = User("joey", 3, True, ["a"])
    pairs = dict(encode_form(u))
    assert pairs["name"] == "joey"
    assert pairs["age"] == "3"
    assert pairs["active"] == "true"


# ---------- 往返一致性（核心契约） ----------

def test_roundtrip_flat() -> None:
    u = User("joey", 3, True)
    back = decode_form(query_string_to_form(form_to_query_string(encode_form(u))), User)
    assert back == u


def test_roundtrip_with_lists() -> None:
    u = User("joey", 3, True, ["a", "b", "c"])
    back = decode_form(query_string_to_form(form_to_query_string(encode_form(u))), User)
    assert back == u


def test_roundtrip_with_nested() -> None:
    """嵌套对象 + 列表 + 单对象，全部一起往返。"""
    u = User(
        "joey", 3, True,
        ["a", "b"],
        [Address("x", "111"), Address("y", "222")],
        Address("h", "999"),
    )
    pairs = encode_form(u)
    back = decode_form(query_string_to_form(form_to_query_string(pairs)), User)
    assert back == u, "往返必须完全一致"


def test_roundtrip_preserves_repeated_keys() -> None:
    """重复键不能在往返中被静默压成最后一个值。"""
    u = User("joey", 3, True, ["a", "b"])
    pairs = encode_form(u)
    assert len([1 for k, _ in pairs if k == "tags"]) == 2
    back = decode_form(pairs, User)
    assert back.tags == ["a", "b"]


def test_query_string_escapes_special_chars() -> None:
    qs = form_to_query_string([("a", "x&y=z"), ("b", "1 2")])
    assert "x%26y%3Dz" in qs
    assert "1%202" in qs


def test_query_string_roundtrip_survives_special_chars() -> None:
    qs = form_to_query_string([("a", "x&y=z"), ("b", "1 2")])
    assert query_string_to_form(qs) == [("a", "x&y=z"), ("b", "1 2")]


# ---------- agent 集成 ----------

def test_agent_registration() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    for name in ("formcodec_encode", "formcodec_decode", "formcodec_to_json",
                 "formcodec_parse_path", "formcodec_validate_pairs"):
        assert reg.has(name), name

    d = Dispatcher(reg)
    r = d.dispatch("formcodec_encode", {"data": {"name": "joey", "tags": ["a", "b"]}})
    assert r.ok
    assert "tags=a" in r.data["query_string"]
    assert "tags=b" in r.data["query_string"]


def test_agent_schemas_are_strict() -> None:
    from agent_core import ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    for spec in reg.specs():
        s = spec.parameters
        assert s["additionalProperties"] is False, spec.name
        assert set(s["required"]) == set(s["properties"]), spec.name


def test_agent_decode_unflattens() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("formcodec_decode", {
        "pairs": [{"key": "a.b", "value": "1"}, {"key": "a.c", "value": "2"}],
    })
    assert r.ok and r.data["data"] == {"a": {"b": "1", "c": "2"}}


def test_agent_parse_path() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("formcodec_parse_path", {"path": "Address[0].Name"})
    assert r.ok
    assert r.data["indices"] == [0]
    assert r.data["fields"] == ["Address", "Name"]


def test_agent_validate_pairs() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)

    ok = d.dispatch("formcodec_validate_pairs", {
        "pairs": [{"key": "status", "value": "active"}],
        "required_fields": ["status"],
        "allowed_values": {"status": ["active", "closed"]},
    })
    assert ok.ok and ok.data["valid"] is True

    bad = d.dispatch("formcodec_validate_pairs", {
        "pairs": [],
        "required_fields": ["status"],
    })
    assert bad.ok and bad.data["valid"] is False
    assert any(i["code"] == "missing_required" for i in bad.data["issues"])

    invalid = d.dispatch("formcodec_validate_pairs", {
        "pairs": [{"key": "status", "value": "x"}],
        "allowed_values": {"status": ["active"]},
    })
    assert invalid.data["valid"] is False


def test_agent_detects_duplicate_keys() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("formcodec_validate_pairs", {
        "pairs": [{"key": "tags", "value": "a"}, {"key": "tags", "value": "b"}],
    })
    assert r.data["duplicate_keys"] == ["tags"]


def test_agent_to_json_casts() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from formcodec.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("formcodec_to_json", {
        "pairs": [{"key": "age", "value": "3"}, {"key": "ok", "value": "true"}],
        "casts": {"age": "int", "ok": "bool"},
    })
    assert r.ok
    assert '"age": 3' in r.data["json"]
    assert '"ok": true' in r.data["json"]


# ---------- CLI ----------

def _cli(args: list[str], stdin: str = "") -> tuple[int, str, str]:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "formcodec.cli", *args],
        input=stdin, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_encode() -> None:
    code, out, _ = _cli(["encode"], '{"name":"joey","tags":["a","b"]}')
    assert code == 0
    assert "name=joey" in out
    assert out.count("tags=") == 2


def test_cli_encode_query_string() -> None:
    code, out, _ = _cli(["encode", "--query-string"], '{"name":"joey","age":3}')
    assert code == 0 and out.strip() == "name=joey&age=3"


def test_cli_decode_with_casts() -> None:
    code, out, _ = _cli(
        ["decode", "--query", "name=joey&age=3", "--cast", "age=int"]
    )
    assert code == 0
    assert '"age": 3' in out


def test_cli_decode_raw_mode() -> None:
    code, out, _ = _cli(["decode", "--query", "a.b=1", "--raw"])
    assert code == 0 and '"a.b"' in out


def test_cli_path() -> None:
    code, out, _ = _cli(["path", "Address[0].Name"])
    assert code == 0 and '"indices"' in out


def test_cli_bad_json_errors() -> None:
    code, _, err = _cli(["encode"], "not json")
    assert code == 1 and "JSON" in err


def test_cli_bad_cast_type_errors() -> None:
    code, _, err = _cli(["decode", "--query", "a=1", "--cast", "a=nope"])
    assert code == 1 and "未知类型" in err


def test_cli_bad_cast_format() -> None:
    code, _, err = _cli(["decode", "--query", "a=1", "--cast", "a"])
    assert code == 1 and "FIELD=TYPE" in err
