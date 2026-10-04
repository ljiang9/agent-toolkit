"""formcodec —— 结构体与表单/查询参数的双向编解码。

复刻自 Go 的 go-playground/form，把 Go 的 struct tag 机制替换为
Python 的类型注解 + `FieldSpec` 元数据，并补上 JSON 互转与校验能力。

>>> from dataclasses import dataclass
>>> @dataclass
... class User:
...     name: str
...     age: int
... decode_form({"name": "joey", "age": "3"}, User)
User(name='joey', age=3)
>>> encode_form(User("joey", 3, ["a", "b"]))
[('name', 'joey'), ('age', '3'), ('tags', 'a'), ('tags', 'b')]
"""

from __future__ import annotations

from .codec import (
    CodecError,
    FieldSpec,
    decode_value,
    encode_value,
    from_bool,
    from_datetime,
    from_json,
    to_bool,
    to_datetime,
    to_float,
    to_int,
    to_json,
    to_list,
)
from .core import (
    decode_form,
    decode_form_safe,
    encode_form,
    form_to_json,
    form_to_query_string,
    query_string_to_form,
    unflatten,
)
from .path import KeyPath, PathError, PathToken, encode_path

__version__ = "0.1.0"

__all__ = [
    "CodecError",
    "FieldSpec",
    "KeyPath",
    "PathError",
    "PathToken",
    "decode_form",
    "decode_form_safe",
    "decode_value",
    "encode_form",
    "encode_path",
    "encode_value",
    "form_to_json",
    "form_to_query_string",
    "from_bool",
    "from_datetime",
    "from_json",
    "query_string_to_form",
    "to_bool",
    "to_datetime",
    "to_float",
    "to_int",
    "to_json",
    "to_list",
    "unflatten",
    "__version__",
]
