"""urlkit —— URL 结构化编解码工具。

复刻自 curl 官方的 trurl（https://github.com/curl/trurl），并补充了
归一化、去重指纹与安全检查三项 agent 场景必需的能力。

>>> from urlkit import parse_url, fingerprint
>>> parse_url("https://a.com/p?x=1").host
'a.com'
>>> fingerprint("https://e.com/?b=2&a=1#top")
'https://e.com/?a=1&b=2'
"""

from __future__ import annotations

from .core import (
    DEFAULT_PORTS,
    UrlParts,
    UrlkitError,
    append_component,
    decode_query,
    encode_query,
    fingerprint,
    get_component,
    iter_batched,
    normalize,
    parse_url,
    redirect,
    set_component,
)
from .safety import check_url_safety

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_PORTS",
    "UrlParts",
    "UrlkitError",
    "append_component",
    "check_url_safety",
    "decode_query",
    "encode_query",
    "fingerprint",
    "get_component",
    "iter_batched",
    "normalize",
    "parse_url",
    "redirect",
    "set_component",
    "__version__",
]
