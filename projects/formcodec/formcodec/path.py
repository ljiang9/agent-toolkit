"""键路径解析：`a.b[0].c`、`m[key]`、嵌套组合。

复刻自 go-playground/form 的路径语法：
    `.`   分隔字段与嵌套结构体
    [n]   访问 slice/array 的第 n 项
    [key] 访问 map 的某个键

这套语法是表单编码的事实标准（PHP、Ruby on Rails、Gin 都用它），
所以解析器与编码器必须严格对称——能解析的就能编码回去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 匹配 `name`、`name[0]`、`name[key]`、`a.b[0][key]`
# 键名允许字母数字下划线连字符点；括号内允许数字或不含方括号的任意键
_TOKEN_RE = re.compile(
    r"""
    (?P<name>[A-Za-z_][\w\-.]*)
    (?:\[(?P<brackets>[^\[\]]*)\])*
    """,
    re.VERBOSE,
)

_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")


class PathError(ValueError):
    """键路径语法错误。"""


@dataclass
class PathToken:
    """路径中的一个片段。

    kind 为 "field" 时用 name，为 "index" 时用 index（int），
    为 "key" 时用 key（str）。
    """

    kind: str  # field | index | key
    name: str = ""
    index: int = 0
    key: str = ""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self.kind == "field":
            return f".{self.name}"
        if self.kind == "index":
            return f"[{self.index}]"
        return f"[{self.key}]"


@dataclass
class KeyPath:
    """解析后的键路径。

    >>> KeyPath.parse("Address[0].Name").tokens
    [PathToken(kind='field', name='Address'), PathToken(kind='index', index=0), PathToken(kind='field', name='Name')]
    """

    raw: str
    tokens: list[PathToken] = field(default_factory=list)

    @property
    def root(self) -> str:
        """最外层字段名。"""
        return self.tokens[0].name if self.tokens else ""

    @property
    def terminal(self) -> str:
        """最内层字段名（不含索引/键）。"""
        for t in reversed(self.tokens):
            if t.kind == "field":
                return t.name
        return ""

    @property
    def indices(self) -> list[int]:
        """路径中所有显式索引，按出现顺序。"""
        return [t.index for t in self.tokens if t.kind == "index"]

    @property
    def keys(self) -> list[str]:
        """路径中所有 map 键。"""
        return [t.key for t in self.tokens if t.kind == "key"]

    @property
    def depth(self) -> int:
        """嵌套深度（字段级）。"""
        return sum(1 for t in self.tokens if t.kind == "field")

    @property
    def has_index(self) -> bool:
        """是否含显式索引——这决定了解码时要不要按位置放置。"""
        return any(t.kind == "index" for t in self.tokens)

    @classmethod
    def parse(cls, path: str) -> "KeyPath":
        """解析键路径。

        >>> KeyPath.parse("user.tags[2]").indices
        [2]
        >>> KeyPath.parse("m[k]").keys
        ['k']
        """
        if not path or not path.strip():
            raise PathError("键路径不能为空")

        raw = path
        text = path.strip()

        # 逐段扫描：先读字段名，再把紧跟的 [..] 全吃掉
        tokens: list[PathToken] = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "[":
                # 允许以 [ 开头（如 [0][1] 表示根数组），但字段名必须先出现
                raise PathError(f"键路径 {raw!r} 在位置 {i} 处出现意外的 '['")
            # 读取字段名（允许其中已含点，如 a.b 会自然分成两段）
            start = i
            while i < n and text[i] not in ".[]":
                i += 1
            name = text[start:i]
            if not name:
                raise PathError(f"键路径 {raw!r} 在位置 {start} 处缺少字段名")
            tokens.append(PathToken(kind="field", name=name))

            # 读取紧随其后的所有 [..]
            while i < n and text[i] == "[":
                close = text.find("]", i)
                if close == -1:
                    raise PathError(f"键路径 {raw!r} 缺少配对的 ']'")
                inner = text[i + 1 : close]
                if inner == "":
                    # 空括号是追加语义：slice[name] 追加到已有列表
                    tokens.append(PathToken(kind="append"))
                elif inner.lstrip("-").isdigit():
                    tokens.append(PathToken(kind="index", index=int(inner)))
                else:
                    tokens.append(PathToken(kind="key", key=inner))
                i = close + 1

            # 跳过分隔点
            if i < n and text[i] == ".":
                i += 1
                if i == n:
                    raise PathError(f"键路径 {raw!r} 以 '.' 结尾")

        if not tokens:
            raise PathError(f"无法解析键路径 {raw!r}")
        return cls(raw=raw, tokens=tokens)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"KeyPath({self.raw!r})"


def encode_path(*parts: object) -> str:
    """由片段反向构造键路径——解析器的逆操作。

    规则依赖**位置**，因为字符串片段在不同位置含义不同：
    - 紧跟字段名 -> map 的键，用 `[key]`（`MapExample` + `key` -> `MapExample[key]`）
    - 紧跟数组索引 -> 嵌套字段名，用 `.Name`（`Address` + `0` + `Name`
      -> `Address[0].Name`）

    >>> encode_path("Address", 0, "Name")
    'Address[0].Name'
    >>> encode_path("MapExample", "key")
    'MapExample[key]'
    >>> encode_path("user", "profile", "city")
    'user[profile][city]'
    """
    if not parts:
        return ""
    out = str(parts[0])
    # 上一个片段是不是数组索引——决定后续字符串用 . 还是 []
    after_index = isinstance(parts[0], int) and not isinstance(parts[0], bool)
    for part in parts[1:]:
        if isinstance(part, int) and not isinstance(part, bool):
            out += f"[{part}]"
            after_index = True
        else:
            out += f".{part}" if after_index else f"[{part}]"
            after_index = False
    return out
