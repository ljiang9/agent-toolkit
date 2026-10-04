"""URL 结构化编解码核心。

复刻自 curl 官方的 trurl（https://github.com/curl/trurl），把 URL 拆成
scheme / user / password / port / host / path / query / fragment 八个组件，
支持按组件读写，再重新组装回去。

设计要点：
- 纯标准库实现，零第三方依赖；
- 保留 query 的**顺序与重复**，因为 `a=1&a=2` 与 `a=2&a=1` 语义可能不同；
- 端口显式区分「未指定」与「显式写了默认端口」，两者重组后表现不同；
- 提供归一化（--sort-query / --qtrim），这是做链接去重指纹的基础。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# 各 scheme 的默认端口。用于 --default-port 归一化与端口显隐判断。
DEFAULT_PORTS = {
    "ftp": 21,
    "ssh": 22,
    "telnet": 23,
    "smtp": 25,
    "dns": 53,
    "http": 80,
    "pop3": 110,
    "imap": 143,
    "snmp": 161,
    "ldap": 389,
    "https": 443,
    "smb": 445,
    "rtsp": 554,
    "ldaps": 636,
    "ws": 80,
    "wss": 443,
}

# 组件名 -> 允许 --set 的别名（对齐 trurl 的叫法）
COMPONENT_ALIASES = {
    "scheme": "scheme",
    "user": "user",
    "username": "user",
    "password": "password",
    "passwd": "password",
    "host": "host",
    "hostname": "host",
    "port": "port",
    "path": "path",
    "fragment": "fragment",
    "frag": "fragment",
    "url": "url",
}

_PORT_RE = re.compile(r"^\d{1,5}$")


class UrlkitError(ValueError):
    """URL 解析或重建失败。"""


@dataclass
class UrlParts:
    """URL 的结构化表示。

    `port` 为 None 表示「未指定端口」，与「显式写了 :443」是不同状态——
    归一化时需要这个区分才能正确去掉默认端口。
    """

    scheme: str = ""
    user: str | None = None
    password: str | None = None
    host: str = ""
    port: int | None = None
    path: str = ""
    # query 保留顺序与重复项：list[tuple[str, str]]
    query: list[tuple[str, str]] = field(default_factory=list)
    fragment: str | None = None

    # ---------- 重组 ----------

    def compose(self, *, default_port: bool = False) -> str:
        """把组件重新组装成 URL 字符串。

        参数
        ----
        default_port:
            为 True 时，即使 scheme 有默认端口也显式写出（`:443`）。
            为 False（默认）时省略默认端口，得到更短的规范形式。
        """
        scheme = self.scheme
        netloc = ""

        # userinfo：user 与 password 都需百分号编码（trurl 的
        # "::moo::" -> "%3a%3amoo%3a%3a" 就是这个规则）
        if self.user is not None or self.password is not None:
            userinfo = ""
            if self.user:
                userinfo += quote(self.user, safe="")
            if self.password is not None:
                userinfo += ":" + quote(self.password, safe="")
            netloc += userinfo + "@"

        netloc += self.host

        if self.port is not None:
            default = DEFAULT_PORTS.get(scheme)
            if default_port or self.port != default:
                netloc += f":{self.port}"

        # scheme 为空时不加 "//"，兼容相对路径形式的输入。
        # 有 scheme 且有 host 时补一个裸 "/"：对齐 trurl 行为——
        # `https://curl.se` 重组后应为 `https://example.com/` 而非 `https://example.com`。
        url = f"{scheme}://{netloc}" if scheme else f"//{netloc}"
        path = self.path
        if not path and scheme and netloc:
            path = "/"
        url += path

        if self.query:
            url += "?" + encode_query(self.query)

        if self.fragment is not None:
            url += "#" + self.fragment

        return url

    def to_dict(self) -> dict[str, object]:
        """转成可 JSON 序列化的 dict（供 agent 工具返回）。"""
        return {
            "scheme": self.scheme,
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "query": [{"key": k, "value": v} for k, v in self.query],
            "fragment": self.fragment,
        }

    # ---------- 查询串操作 ----------

    def get_query(self, key: str) -> list[str]:
        """取某个 key 的所有值（保留重复）。"""
        return [v for k, v in self.query if k == key]

    def get_query_first(self, key: str, default: str | None = None) -> str | None:
        vals = self.get_query(key)
        return vals[0] if vals else default

    def set_query(self, key: str, value: str) -> None:
        """设置 key：已存在则全部替换，不存在则追加到末尾。"""
        if any(k == key for k, _ in self.query):
            first = True
            new: list[tuple[str, str]] = []
            for k, v in self.query:
                if k == key:
                    if first:
                        new.append((key, value))
                        first = False
                    # 其余同名项丢弃
                else:
                    new.append((k, v))
            self.query = new
        else:
            self.query.append((key, value))

    def add_query(self, key: str, value: str) -> None:
        """追加，不替换已有同键项。"""
        self.query.append((key, value))

    def remove_query(self, key: str) -> int:
        """删除某个 key 的所有项，返回删除数量。"""
        before = len(self.query)
        self.query = [(k, v) for k, v in self.query if k != key]
        return before - len(self.query)

    def trim_query(self, patterns: list[str]) -> int:
        """删除 key 匹配任一 pattern 的项，支持 `*` 通配。返回删除数量。

        >>> p = parse_url("https://e.com/?a=1&utm_source=x&b=2")
        >>> p.trim_query(["utm_*"])
        1
        >>> p.compose()
        'https://e.com/?a=1&b=2'
        """
        removed = 0
        kept: list[tuple[str, str]] = []
        for k, v in self.query:
            if any(fnmatch_key(k, p) for p in patterns):
                removed += 1
            else:
                kept.append((k, v))
        self.query = kept
        return removed

    def sort_query(self) -> None:
        """按 key、再按 value 排序。

        这是链接去重指纹的关键：`?a=1&b=2` 与 `?b=2&a=1` 排序后相同。
        注意排序会丢失同名项的相对顺序。
        """
        self.query.sort()

    def dedup_query(self) -> int:
        """去掉完全重复的 (key, value) 项，保留首次出现。返回删除数量。"""
        seen: set[tuple[str, str]] = set()
        kept: list[tuple[str, str]] = []
        for item in self.query:
            if item not in seen:
                seen.add(item)
                kept.append(item)
        removed = len(self.query) - len(kept)
        self.query = kept
        return removed


def fnmatch_key(key: str, pattern: str) -> bool:
    """key 是否匹配 pattern，支持 `*` 通配与 `?` 单字符。

    >>> fnmatch_key("utm_source", "utm_*")
    True
    >>> fnmatch_key("utm_source", "*_id")
    True
    >>> fnmatch_key("page", "p?ge")
    True
    >>> fnmatch_key("page", "pa")
    False
    """
    regex = "".join(
        ".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern
    )
    # fullmatch 而非 match+$：$ 还能匹配末尾换行符前的位置，
    # fullmatch 才能保证整串匹配，避免 `utm_*` 意外命中 `utm_source\n`
    return re.fullmatch(regex, key, re.IGNORECASE) is not None


def normalize_path(path: str) -> str:
    """解析路径中的点号段，RFC 3986 的 remove_dot_segments。

    trurl 的实测行为：`https://curl.se/we/../are.html` 重组后得到
    `https://curl.se/are.html` —— 即点号段在解析阶段就被消解。

    >>> normalize_path("/we/../are.html")
    '/are.html'
    >>> normalize_path("/a/b/./c")
    '/a/b/c'
    >>> normalize_path("/a/../../b")
    '/b'
    >>> normalize_path("")
    ''
    """
    if not path:
        return path

    # 保留结尾斜杠的语义：/a/b/ 与 /a/b 在服务端可能不同
    trailing_slash = path.endswith("/")
    segments: list[str] = []

    for seg in path.split("/"):
        if seg == ".":
            # 单点：跳过，但若是最后一个则意味着结尾斜杠
            continue
        if seg == "..":
            if segments and segments[-1] != "..":
                segments.pop()
            elif not path.startswith("/"):
                # 相对路径开头的 .. 无法消解，保留
                segments.append(seg)
            # 绝对路径的 .. 越过根时按 RFC 丢弃
            continue
        segments.append(seg)

    result = "/".join(segments)
    if not result:
        return "/" if path.startswith("/") else ""
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    if trailing_slash and not result.endswith("/"):
        result += "/"
    return result


def decode_query(query: str, *, separator: str | None = None) -> list[tuple[str, str]]:
    """解析查询串为 (key, value) 列表。

    参数
    ----
    separator:
        查询串内的参数分隔符。默认 `&`；若 URL 形如 `?a=1;b=2`
        （部分站点用 `;`），可传 `;` 解析。也可自动尝试。
    """
    if not query:
        return []
    seps = [separator] if separator else ["&", ";"]
    out: list[tuple[str, str]] = []
    for chunk in _split_query(query, seps):
        if not chunk:
            continue
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            out.append((unquote(k.replace("+", " ")), unquote(v.replace("+", " "))))
        else:
            # 无 `=` 的裸参数，按 RFC 约定 key 为空值
            out.append((unquote(chunk.replace("+", " ")), ""))
    return out


def _split_query(query: str, seps: list[str]) -> list[str]:
    """按给定分隔符切分查询串。"""
    pattern = "[" + re.escape("".join(seps)) + "]"
    return re.split(pattern, query)


def encode_query(items: list[tuple[str, str]]) -> str:
    """把 (key, value) 列表编码回查询串。

    空格编码为 `%20` 而非 `+`：URL 标准化（RFC 3986）用前者，
    且比 `+` 更不容易在跨系统传递时被误解。
    """
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in items)


def parse_url(url: str, *, accept_space: bool = False) -> UrlParts:
    """把 URL 字符串解析成 UrlParts。

    参数
    ----
    accept_space:
        为 True 时把未编码的空格编码成 `%20`。默认 False —— 遇到裸空格
        直接报错，因为那通常意味着上游拼接有 bug，静默修复会掩盖问题。
    """
    if not isinstance(url, str):
        raise UrlkitError(f"URL 必须是字符串，收到 {type(url).__name__}")

    raw = url.strip()
    if " " in raw and not accept_space:
        raise UrlkitError(
            f"URL 含未编码的空格：{raw!r}；如需自动编码请传 accept_space=True"
        )
    if " " in raw:
        raw = raw.replace(" ", "%20")

    try:
        split = urlsplit(raw)
    except ValueError as exc:
        raise UrlkitError(f"URL 解析失败：{exc}") from exc

    # urlsplit 把 userinfo/host:port 混在 netloc 里，这里按 RFC 3986 逐层拆开。
    # 注意：不能直接用 netloc —— 它含端口（"a.com:8080"），也含 userinfo。
    # hostname 由标准库负责剥端口并保留 IPv6 方括号，是最可靠的取值来源。
    userinfo = None
    if "@" in split.netloc:
        userinfo, _, _ = split.netloc.rpartition("@")

    user: str | None = None
    password: str | None = None
    if userinfo:
        if ":" in userinfo:
            raw_user, _, raw_pass = userinfo.partition(":")
            user = unquote(raw_user)
            password = unquote(raw_pass)
        else:
            user = unquote(userinfo)

    # hostname 自动处理端口剥离与 IPv6 方括号
    host = split.hostname or ""
    # 有些 URL 带裸 @（如 mailto 风格），urlsplit 会把它塞进 path，这里兜底
    if not host and split.netloc:
        host = split.netloc.split("@")[-1].split(":")[0]

    port: int | None = None
    if split.port is not None:
        if not _PORT_RE.match(str(split.port)):
            raise UrlkitError(f"非法端口号：{split.port}")
        port = split.port

    return UrlParts(
        scheme=split.scheme,
        user=user,
        password=password,
        host=host,
        port=port,
        path=normalize_path(split.path),
        query=decode_query(split.query),
        fragment=split.fragment if split.fragment or "#" in raw else None,
    )


def normalize(url: str, *, strip_default_port: bool = True, sort_query: bool = False) -> str:
    """归一化 URL，得到稳定的比较形式。

    这是链接去重的第一步：把 `https://e.com:443/a` 与 `https://e.com/a`
    归一到同一个字符串，再配合 `sort_query` 与去默认端口即可做指纹。

    >>> normalize("https://Example.COM:443/a?b=1")
    'https://example.com/a?b=1'
    """
    parts = parse_url(url, accept_space=True)
    parts.host = parts.host.lower()
    if strip_default_port and parts.port == DEFAULT_PORTS.get(parts.scheme):
        parts.port = None
    if sort_query:
        parts.sort_query()
        parts.dedup_query()
    return parts.compose()


def fingerprint(url: str) -> str:
    """生成用于去重/聚类的规范化指纹。

    比 `normalize` 更激进：排序 query、去重、去默认端口、去 fragment
    （fragment 通常不影响服务端资源，故对同一资源是冗余的）。

    >>> fingerprint("https://e.com/?b=2&a=1#top")
    'https://e.com/?a=1&b=2'
    """
    parts = parse_url(url, accept_space=True)
    parts.host = parts.host.lower()
    if parts.port == DEFAULT_PORTS.get(parts.scheme):
        parts.port = None
    parts.sort_query()
    parts.dedup_query()
    parts.fragment = None
    # 尾部斜杠归一：`/a` 与 `/a/` 对多数服务端等价
    if len(parts.path) > 1 and parts.path.endswith("/"):
        parts.path = parts.path.rstrip("/") or "/"
    return parts.compose()


# ---------- 按组件读写 ----------


def get_component(url: str, component: str) -> str | int | None:
    """读取单个组件的值。

    >>> get_component("https://curl.se/we/are.html", "path")
    '/we/are.html'
    >>> get_component("https://curl.se/we/are.html", "port")
    443
    """
    parts = parse_url(url, accept_space=True)
    comp = _resolve_component(component, parts)
    return comp


def set_component(url: str, component: str, value: str) -> str:
    """设置单个组件并返回重组后的 URL。

    对齐 trurl 的 `--set` 语义：可写回 URL 本身。

    >>> set_component("https://curl.se", "host", "example.com")
    'https://example.com/'
    >>> set_component("https://curl.se", "port", "8080")
    'https://curl.se:8080/'
    """
    parts = parse_url(url, accept_space=True)
    _apply_set(parts, component, value)
    return parts.compose()


def append_component(url: str, component: str, value: str) -> str:
    """追加到组件而非替换。

    path 用 `/` 拼接（避免双斜杠），query 用 `&` 追加。

    >>> append_component("https://curl.se/hello", "path", "you")
    'https://curl.se/hello/you'
    >>> append_component("https://curl.se?name=hello", "query", "search=string")
    'https://curl.se/?name=hello&search=string'
    """
    parts = parse_url(url, accept_space=True)

    if component == "path":
        base = parts.path.rstrip("/")
        addition = value.lstrip("/")
        parts.path = f"{base}/{addition}"
    elif component == "query":
        if not value:
            pass
        elif "=" in value:
            parts.add_query(*value.partition("=")[::2])
        else:
            parts.add_query(value, "")
    elif component == "fragment":
        base = parts.fragment or ""
        parts.fragment = f"{base}{value}"
    else:
        raise UrlkitError(f"组件 {component!r} 不支持 append 操作")

    return parts.compose()


def _resolve_component(component: str, parts: UrlParts):
    """解析组件别名并取值。"""
    key = COMPONENT_ALIASES.get(component.lower())
    if key is None:
        raise UrlkitError(
            f"未知组件 {component!r}；可用组件：{sorted(set(COMPONENT_ALIASES))}"
        )
    if key == "scheme":
        return parts.scheme or None
    if key == "port":
        # 未显式指定端口时回落到 scheme 默认端口，对齐 trurl 的 --get '{port}'
        return parts.port if parts.port is not None else DEFAULT_PORTS.get(parts.scheme)
    return getattr(parts, key)


def _apply_set(parts: UrlParts, component: str, value: str) -> None:
    """解析组件别名并赋值。"""
    key = COMPONENT_ALIASES.get(component.lower())
    if key is None:
        raise UrlkitError(
            f"未知组件 {component!r}；可用组件：{sorted(set(COMPONENT_ALIASES))}"
        )
    if key == "port":
        # 允许空值表示「去掉端口」，trurl 用 --set port= 支持这种写法
        parts.port = int(value) if value else None
    elif key == "url":
        new = parse_url(value, accept_space=True)
        parts.__dict__.update(new.__dict__)
    else:
        setattr(parts, key, value or None)


def redirect(url: str, new_path: str) -> str:
    """把 URL 的路径替换为目标路径，其余组件不变。

    对齐 trurl 的 `--redirect`：目标路径相对于**当前所在目录**解析，
    所以 `/we/are.html` 重定向到 `here.html` 得到 `/we/here.html` 而非 `/here.html`。

    >>> redirect("https://curl.se/we/are.html", "here.html")
    'https://curl.se/we/here.html'
    >>> redirect("https://a.com/old", "/new")
    'https://a.com/new'
    """
    parts = parse_url(url, accept_space=True)
    if new_path.startswith("/"):
        # 绝对路径：直接替换
        parts.path = normalize_path(new_path)
    else:
        # 相对路径：基于当前所在目录解析（对齐 trurl）
        base = parts.path.rsplit("/", 1)[0] if "/" in parts.path else ""
        parts.path = normalize_path(f"{base}/{new_path}")
    return parts.compose()


def iter_batched(lines: Iterator[str]) -> Iterator[str]:
    """流式处理多行输入，跳过空行与以 # 开头的注释行。

    对齐 trurl 的 `--url-file -` 从 stdin 读入的行为，便于在管道里流式处理。
    """
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped
