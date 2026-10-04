"""urlkit 测试。

测试用例直接对齐 trurl 官方 README 中的行为契约，确保复刻语义一致。
"""

from __future__ import annotations

import pytest

from urlkit import (
    UrlkitError,
    append_component,
    check_url_safety,
    fingerprint,
    get_component,
    normalize,
    parse_url,
    redirect,
    set_component,
)
from urlkit.core import decode_query, encode_query, fnmatch_key


# ---------- 解析 ----------

def test_parse_basic() -> None:
    p = parse_url("https://user:pw@curl.se:8080/we/are.html?a=1&b=2#frag")
    assert p.scheme == "https"
    assert p.user == "user"
    assert p.password == "pw"
    assert p.host == "curl.se"
    assert p.port == 8080
    assert p.path == "/we/are.html"
    assert p.query == [("a", "1"), ("b", "2")]
    assert p.fragment == "frag"


def test_parse_no_query() -> None:
    p = parse_url("https://curl.se/we/are.html")
    assert p.query == []
    assert p.fragment is None
    assert p.port is None


def test_parse_percent_encoded_userinfo() -> None:
    """trurl: "::moo::" 重组后应为 "%3a%3amoo%3a%3a"。

    Python 的 quote() 产出大写 %3A，与 trurl 的小写形式在语义上等价
    （同一份百分号编码，大小写不敏感），因此断言按语义而非字节比较。
    """
    url = set_component("https://fake.host/hello#frag", "user", "::moo::")
    # 只检查 userinfo 段（@ 之前），避免把 host 一起大写
    userinfo = url.split("@", 1)[0].removeprefix("https://")
    assert userinfo.upper() == "%3A%3AMOO%3A%3A"
    assert userinfo.lower() == "%3a%3amoo%3a%3a"
    assert url.endswith("@fake.host/hello#frag")
    # 再解析回来应还原成原值——这才是真正要保证的往返一致
    assert parse_url(url).user == "::moo::"
    assert parse_url(url).host == "fake.host"


def test_parse_rejects_bare_space_by_default() -> None:
    with pytest.raises(UrlkitError, match="未编码的空格"):
        parse_url("https://curl.se/this has space/index.html")


def test_parse_accept_space_option() -> None:
    p = parse_url("https://curl.se/this has space/index.html", accept_space=True)
    assert p.path == "/this%20has%20space/index.html"


def test_parse_rejects_non_string() -> None:
    with pytest.raises(UrlkitError, match="必须是字符串"):
        parse_url(None)  # type: ignore[arg-type]


# ---------- 组装 ----------

def test_compose_roundtrip() -> None:
    for url in [
        "https://curl.se/",
        "http://a.com/p?x=1",
        "https://u:p@h.io:8443/a/b?c=d#e",
        "ftp://files.example.org/pub/data.txt",
    ]:
        assert parse_url(url, accept_space=True).compose() == url


def test_compose_omits_default_port() -> None:
    p = parse_url("https://curl.se/we/are.html")
    assert p.compose() == "https://curl.se/we/are.html"
    p.port = 443
    assert p.compose() == "https://curl.se/we/are.html"  # 443 是 https 默认端口，省略
    assert p.compose(default_port=True) == "https://curl.se:443/we/are.html"


def test_compose_keeps_non_default_port() -> None:
    p = parse_url("https://curl.se:8443/x")
    assert p.compose() == "https://curl.se:8443/x"


# ---------- get / set（对齐 trurl） ----------

def test_set_host() -> None:
    """trurl: --url https://curl.se --set host=example.com"""
    assert set_component("https://curl.se", "host", "example.com") == "https://example.com/"


def test_set_scheme_without_url() -> None:
    """trurl: --set host=example.com --set scheme=ftp → ftp://example.com/"""
    url = set_component("https://example.com/", "host", "example.com")
    url = set_component(url, "scheme", "ftp")
    assert url == "ftp://example.com/"


def test_set_port() -> None:
    assert set_component("https://curl.se", "port", "8080") == "https://curl.se:8080/"


def test_set_port_empty_removes() -> None:
    url = set_component("https://curl.se:8080/x", "port", "")
    assert url == "https://curl.se/x"


def test_set_path_normalizes_dotdot() -> None:
    """trurl: --url https://curl.se/we/../are.html --set port=8080 → :8080/are.html"""
    url = set_component("https://curl.se/we/../are.html", "port", "8080")
    assert url == "https://curl.se:8080/are.html"


def test_get_component() -> None:
    """trurl: --get '{path}' 与 --get '{port}'"""
    url = "https://curl.se/we/are.html"
    assert get_component(url, "path") == "/we/are.html"
    assert get_component(url, "port") == 443  # 未显式指定时回落到默认端口
    assert get_component(url, "host") == "curl.se"
    assert get_component(url, "scheme") == "https"


def test_get_component_explicit_port() -> None:
    assert get_component("https://curl.se:8443/x", "port") == 8443


def test_unknown_component_raises() -> None:
    with pytest.raises(UrlkitError, match="未知组件"):
        get_component("https://a.com", "nope")


def test_component_aliases() -> None:
    url = "https://curl.se/x"
    assert get_component(url, "hostname") == get_component(url, "host")
    assert get_component(url, "frag") == get_component(url, "fragment")
    assert get_component(url, "username") == get_component(url, "user")


# ---------- append / redirect ----------

def test_append_path() -> None:
    """trurl: --url https://curl.se/hello --append path=you"""
    assert append_component("https://curl.se/hello", "path", "you") == "https://curl.se/hello/you"


def test_append_path_no_double_slash() -> None:
    assert append_component("https://curl.se/hello/", "path", "you") == "https://curl.se/hello/you"


def test_append_query() -> None:
    """trurl: --url "https://curl.se?name=hello" --append query=search=string"""
    url = append_component("https://curl.se?name=hello", "query", "search=string")
    assert url == "https://curl.se/?name=hello&search=string"


def test_append_fragment() -> None:
    assert append_component("https://a.com/p", "fragment", "sec") == "https://a.com/p#sec"


def test_append_unsupported_component() -> None:
    with pytest.raises(UrlkitError, match="不支持 append"):
        append_component("https://a.com", "host", "x")


def test_redirect() -> None:
    """trurl: --url https://curl.se/we/are.html --redirect here.html"""
    assert redirect("https://curl.se/we/are.html", "here.html") == "https://curl.se/we/here.html"


def test_redirect_keeps_other_components() -> None:
    url = redirect("https://a.com:8443/old?x=1#f", "/new")
    assert url == "https://a.com:8443/new?x=1#f"


# ---------- query 操作（对齐 trurl） ----------

def test_query_preserves_order_and_duplicates() -> None:
    """query 的顺序与重复项必须保留，因为语义可能不同。"""
    p = parse_url("https://example.com?b=a&c=b&a=c")
    assert p.query == [("b", "a"), ("c", "b"), ("a", "c")]


def test_sort_query() -> None:
    """trurl: --sort-query"""
    p = parse_url("https://example.com?b=a&c=b&a=c")
    p.sort_query()
    assert p.compose() == "https://example.com/?a=c&b=a&c=b"


def test_qtrim_wildcard() -> None:
    """trurl: --qtrim "utm_*" """
    p = parse_url("https://curl.se?search=hey&utm_source=tracker")
    assert p.trim_query(["utm_*"]) == 1
    assert p.compose() == "https://curl.se/?search=hey"


def test_qtrim_exact() -> None:
    p = parse_url("https://curl.se?search=fool&page=5")
    assert p.trim_query(["search"]) == 1
    assert p.compose() == "https://curl.se/?page=5"


def test_get_query_by_key() -> None:
    """trurl: -g '{query:a}'"""
    p = parse_url("https://example.com?a=home&here=now&thisthen")
    assert p.get_query("a") == ["home"]
    assert p.get_query_first("a") == "home"
    assert p.get_query_first("zzz", "fallback") == "fallback"


def test_set_query_replaces_all() -> None:
    p = parse_url("https://e.com/?a=1&b=2&a=3")
    p.set_query("a", "9")
    assert p.query == [("a", "9"), ("b", "2")]


def test_set_query_appends_when_absent() -> None:
    p = parse_url("https://e.com/?a=1")
    p.set_query("b", "2")
    assert p.query == [("a", "1"), ("b", "2")]


def test_add_query_keeps_duplicates() -> None:
    p = parse_url("https://e.com/?a=1")
    p.add_query("a", "2")
    assert p.get_query("a") == ["1", "2"]


def test_dedup_query() -> None:
    p = parse_url("https://e.com/?a=1&a=1&b=2")
    assert p.dedup_query() == 1
    assert p.query == [("a", "1"), ("b", "2")]


def test_encode_decode_query_roundtrip() -> None:
    items = [("a", "1"), ("key with space", "v&x")]
    assert decode_query(encode_query(items)) == items


def test_decode_query_bare_key() -> None:
    """无 '=' 的裸参数按空值处理。"""
    assert decode_query("flag&a=1") == [("flag", ""), ("a", "1")]


def test_decode_query_semicolon_separator() -> None:
    """trurl: --query-separator ';' 支持 a=1;b=2 这种写法。"""
    assert decode_query("search=fool;page=5", separator=";") == [
        ("search", "fool"),
        ("page", "5"),
    ]


def test_decode_query_plus_is_space() -> None:
    assert decode_query("q=a+b") == [("q", "a b")]


# ---------- 归一化与指纹 ----------

def test_normalize_lowercases_host_and_strips_default_port() -> None:
    assert normalize("https://Example.COM:443/a?b=1") == "https://example.com/a?b=1"


def test_normalize_keeps_non_default_port() -> None:
    assert normalize("https://Example.com:8443/a") == "https://example.com:8443/a"


def test_normalize_sort_query() -> None:
    assert normalize("https://e.com/?b=2&a=1", sort_query=True) == "https://e.com/?a=1&b=2"


def test_fingerprint_aggressive() -> None:
    """指纹比 normalize 更激进，用于聚合同一资源的链接变体。"""
    assert fingerprint("https://e.com/?b=2&a=1#top") == "https://e.com/?a=1&b=2"


def test_fingerprint_ignores_trailing_slash() -> None:
    assert fingerprint("https://e.com/a/") == fingerprint("https://e.com/a")


def test_fingerprint_groups_variants() -> None:
    """同一资源的不同写法应得到相同指纹。"""
    variants = [
        "https://e.com/a?x=1&y=2",
        "https://e.com/a?y=2&x=1",
        "https://E.com/a?x=1&y=2#frag",
    ]
    fps = {fingerprint(v) for v in variants}
    assert len(fps) == 1, f"指纹应一致，实际得到 {fps}"


# ---------- 通配匹配 ----------

def test_fnmatch_key() -> None:
    assert fnmatch_key("utm_source", "utm_*")
    assert fnmatch_key("user_id", "*_id")
    assert fnmatch_key("page", "p?ge")
    assert not fnmatch_key("page", "pa")
    assert not fnmatch_key("utm_source", "*_id")  # utm_source 并非以 _id 结尾
    assert fnmatch_key("UTM_SOURCE", "utm_*")  # 大小写不敏感


# ---------- 安全检查 ----------

def test_safety_https_ok() -> None:
    r = check_url_safety("https://example.com/a")
    assert r["safe"] is True
    assert r["issues"] == []


def test_safety_rejects_javascript_scheme() -> None:
    r = check_url_safety("javascript:alert(1)")
    assert r["safe"] is False
    assert any(i["code"] == "scheme_not_allowed" for i in r["issues"])


def test_safety_rejects_file_scheme() -> None:
    assert check_url_safety("file:///etc/passwd")["safe"] is False


def test_safety_rejects_loopback() -> None:
    r = check_url_safety("http://127.0.0.1:8080/admin")
    assert r["safe"] is False
    assert any(i["code"] == "private_ip" for i in r["issues"])


def test_safety_rejects_localhost_name() -> None:
    assert check_url_safety("http://localhost/x")["safe"] is False


def test_safety_rejects_cloud_metadata() -> None:
    r = check_url_safety("http://169.254.169.254/latest/meta-data/")
    assert r["safe"] is False
    assert any(i["code"] == "cloud_metadata" for i in r["issues"])


def test_safety_rejects_private_ranges() -> None:
    for host in ["10.0.0.1", "192.168.1.1", "172.16.0.1", "[::1]"]:
        assert check_url_safety(f"http://{host}/")["safe"] is False, host


def test_safety_warns_inline_credentials() -> None:
    r = check_url_safety("https://user:pass@example.com/")
    assert r["safe"] is True  # 只是 warning，不阻断
    assert any(i["code"] == "inline_credentials" for i in r["issues"])


def test_safety_warns_unusual_port() -> None:
    r = check_url_safety("https://example.com:9999/")
    assert r["safe"] is True
    assert any(i["code"] == "unusual_port" for i in r["issues"])


def test_safety_handles_unparseable() -> None:
    r = check_url_safety("not a url")
    assert r["safe"] is False
    # 无 scheme 的裸文本应被拦下
    assert any(i["code"] == "missing_scheme" for i in r["issues"])


def test_safety_rejects_missing_host() -> None:
    """`http://` 这类无主机名的输入必须拦下——否则等于放过了无法定位目标的 URL。"""
    r = check_url_safety("http://")
    assert r["safe"] is False
    assert any(i["code"] == "missing_host" for i in r["issues"])


def test_safety_handles_port_overflow() -> None:
    r = check_url_safety("https://h:99999/")
    assert r["safe"] is False
    assert any(i["code"] == "parse_failed" for i in r["issues"])


def test_safety_rejects_empty_host_with_path() -> None:
    assert check_url_safety("https:///just/a/path")["safe"] is False


def test_safety_rejects_missing_scheme() -> None:
    r = check_url_safety("//example.com/x")
    assert r["safe"] is False


# ---------- agent 集成 ----------

def test_agent_registration() -> None:
    """工具应能注册进 agent-core 的注册表并被调用。"""
    from agent_core import Dispatcher, ToolRegistry

    from urlkit.agent import register

    reg = ToolRegistry()
    register(reg)

    assert reg.has("urlkit_parse")
    assert reg.has("urlkit_fingerprint")
    assert reg.has("urlkit_safety_check")

    d = Dispatcher(reg)
    r = d.dispatch("urlkit_parse", {"url": "https://curl.se/we/are.html"})
    assert r.ok and r.data["host"] == "curl.se"

    r2 = d.dispatch("urlkit_fingerprint", {"url": "https://e.com/?b=2&a=1#top"})
    assert r2.ok and r2.data["fingerprint"] == "https://e.com/?a=1&b=2"

    r3 = d.dispatch("urlkit_safety_check", {"url": "javascript:alert(1)"})
    assert r3.ok and r3.data["safe"] is False


def test_agent_schemas_are_strict() -> None:
    """所有工具的 schema 必须满足 strict 模式的两条硬规则。"""
    from agent_core import ToolRegistry

    from urlkit.agent import register

    reg = ToolRegistry()
    register(reg)
    for spec in reg.specs():
        schema = spec.parameters
        assert schema["additionalProperties"] is False, spec.name
        assert set(schema["required"]) == set(schema["properties"]), spec.name


def test_agent_handles_bad_args_gracefully() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from urlkit.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)

    # 缺必填参数：返回错误而不是抛异常
    r = d.dispatch("urlkit_parse", {})
    assert r.ok is False
    assert "参数校验失败" in r.error

    # 非法枚举值
    r2 = d.dispatch("urlkit_get_component", {"url": "https://a.com", "component": "bogus"})
    assert r2.ok is False


# ---------- CLI ----------

def test_cli_set_host(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://curl.se", "--set", "host=example.com"]) == 0
    assert capsys.readouterr().out.strip() == "https://example.com/"


def test_cli_get_component(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://curl.se/we/are.html", "--get", "path"]) == 0
    assert capsys.readouterr().out.strip() == "/we/are.html"


def test_cli_redirect(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://curl.se/we/are.html", "--redirect", "here.html"]) == 0
    assert capsys.readouterr().out.strip() == "https://curl.se/we/here.html"


def test_cli_append(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://curl.se/hello", "--append", "path=you"]) == 0
    assert capsys.readouterr().out.strip() == "https://curl.se/hello/you"


def test_cli_qtrim(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://curl.se?search=hey&utm_source=tracker", "--qtrim", "utm_*"]) == 0
    assert capsys.readouterr().out.strip() == "https://curl.se/?search=hey"


def test_cli_sort_query(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://example.com?b=a&c=b&a=c", "--sort-query"]) == 0
    assert capsys.readouterr().out.strip() == "https://example.com/?a=c&b=a&c=b"


def test_cli_subcommand_parse(capsys) -> None:
    from urlkit.cli import main

    assert main(["parse", "https://a.com/p?x=1"]) == 0
    out = capsys.readouterr().out
    assert '"host": "a.com"' in out


def test_cli_subcommand_check(capsys) -> None:
    from urlkit.cli import main

    assert main(["check", "javascript:alert(1)"]) == 0
    assert '"safe": false' in capsys.readouterr().out


def test_cli_bad_set_format(capsys) -> None:
    from urlkit.cli import main

    assert main(["https://a.com", "--set", "host"]) == 1
    assert "COMPONENT=VALUE" in capsys.readouterr().err


def test_cli_no_args_prints_help(capsys) -> None:
    from urlkit.cli import main

    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()
