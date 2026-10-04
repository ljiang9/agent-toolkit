# formcodec

结构体与表单 / 查询参数的双向编解码，自动类型转换与嵌套结构还原。

复刻自 Go 的 [go-playground/form](https://github.com/go-playground/form)（930 star），
把 Go 的 struct tag 机制替换为 Python 的**类型注解 + `FieldSpec` 元数据**，
并补上 JSON 互转、字段校验、agent 接入能力。零第三方依赖。

## 核心契约：往返一致

```
decode_form(encode_form(x), type(x)) == x
```

这条性质一旦破坏，整个库就没用了，所以测试里反复验证 —— 包括嵌套对象、
数组、map、含特殊字符的值。

## 快速开始

```bash
pip install -e .

echo '{"name":"joey","tags":["a","b"]}' | formcodec encode
# name=joey
# tags=a
# tags=b

formcodec decode --query 'name=joey&age=3' --cast age=int
# {"name": "joey", "age": 3}

formcodec path 'Address[0].Name'
```

## 作为库使用

```python
from dataclasses import dataclass, field
from formcodec import decode_form, encode_form, FieldSpec

@dataclass
class User:
    name: str
    age: int
    active: bool
    tags: list[str] = field(default_factory=list)

# 解码：字符串自动转成 int / bool / list
u = decode_form({"name": "joey", "age": "3", "active": "true",
                 "tags": ["a", "b"]}, User)
# User(name='joey', age=3, active=True, tags=['a', 'b'])

# 编码
encode_form(u)
# [('name','joey'), ('age','3'), ('active','true'), ('tags','a'), ('tags','b')]
```

## 键路径语法

复刻原项目的表单路径约定：

| 语法 | 含义 | 示例 |
|---|---|---|
| `.` | 嵌套字段 | `user.name` |
| `[0]` | 数组索引 | `Address[0].Name` |
| `[key]` | map 的键 | `MapExample[key]` |
| 组合 | 任意嵌套 | `NestedMap[k1][k2]` |

```python
from formcodec import KeyPath

p = KeyPath.parse("Address[0].Name")
p.root        # 'Address'
p.terminal    # 'Name'
p.indices     # [0]
p.depth       # 2
```

## 嵌套结构

表单里嵌套字段天然是扁平的 `parent.child`，解码时会自动还原成嵌套对象：

```python
from dataclasses import dataclass

@dataclass
class Address:
    name: str
    phone: str

@dataclass
class User:
    name: str
    addresses: list[Address] = field(default_factory=list)
    home: Address | None = None

# 数组 -> 重复键
encode_form({"tags": ["a", "b"]})
# [('tags','a'), ('tags','b')]

# 嵌套对象 -> 点号
encode_form({"home": Address("h", "1")})
# [('home.name','h'), ('home.phone','1')]

# 对象数组 -> 索引点号
encode_form({"addresses": [Address("x","1"), Address("y","2")]})
# [('addresses[0].name','x'), ('addresses[0].phone','1'),
#  ('addresses[1].name','y'), ('addresses[1].phone','2')]

# 解码时自动还原
u = decode_form([("addresses[0].name","x"), ("addresses[0].phone","1")], User)
u.addresses   # [Address('x','1')]
```

## 支持的类型

| 类别 | 类型 |
|---|---|
| 基础 | `str` `bool` `int` `float` |
| 容器 | `list` `set` `tuple` `dict` |
| 时间 | `datetime` `date` `time` `timedelta` |
| 其它 | `Enum` `UUID` `IPv4Address` `IPv6Address` |
| 嵌套 | `dataclass`（可无限嵌套） |
| 组合 | `X \| None`（可空） |

布尔转换不接受 Python 的 `bool(str)` 语义 —— `bool("false")` 是 `True`，
显然不符合预期，所以显式处理：

```python
from formcodec import to_bool
to_bool("true"), to_bool("yes"), to_bool("1")    # True True True
to_bool("false"), to_bool("no"), to_bool("0")    # False False False
to_bool("maybe")                                  # 抛 CodecError
```

时长支持 `1h30m`、`90s`、`1.5h` 这类人类可读写法：

```python
from formcodec import decode_value
from datetime import timedelta
decode_value("1h30m", timedelta)     # timedelta(hours=1, minutes=30)
decode_value("90s", timedelta)       # timedelta(seconds=90)
```

## 字段元数据

用 `FieldSpec` 声明默认值、允许取值、长度约束：

```python
from dataclasses import dataclass
from formcodec import FieldSpec, decode_form

@dataclass
class Query:
    status: str
    limit: int = 20
    __form_spec__ = {
        "status": FieldSpec(options=("active", "closed")),   # 枚举校验
        "limit":  FieldSpec(default=50),                     # 缺省值
    }

decode_form({"status": "active"}, Query).limit               # 50
decode_form({"status": "x"}, Query)                          # 抛 CodecError
```

也支持字段级 metadata 与 `ignore`：

```python
@dataclass
class Form:
    name: str
    _csrf: str = field(default="", metadata={"form": FieldSpec(ignore=True)})
```

## 容错解码

```python
from dataclasses import dataclass
from formcodec import decode_form_safe

@dataclass
class T:
    age: int

obj, errors = decode_form_safe({"age": "bad"}, T)
# obj is None
# errors = ["参数错误：无法把 'bad' 解析为整数..."]
```

agent 场景常用 —— 字段类型不对时不会抛出异常中断流程，而是返回错误列表，
让模型自己纠正参数后重试。

## 作为 agent 工具

注册 5 个工具：

```python
from agent_core import ToolRegistry, Dispatcher
from formcodec.agent import register

reg = ToolRegistry()
register(reg)
d = Dispatcher(reg)

d.dispatch("formcodec_encode", {"data": {"name": "joey", "tags": ["a", "b"]}})
# {"ok": true, "data": {"query_string": "name=joey&tags=a&tags=b", ...}}
```

| 工具 | 用途 |
|---|---|
| `formcodec_encode` | 对象 → 表单键值对 / query 串 |
| `formcodec_decode` | 键值对 → 嵌套结构 |
| `formcodec_to_json` | 键值对 → JSON，按声明类型转换 |
| `formcodec_parse_path` | 解析键路径语法 |
| `formcodec_validate_pairs` | 校验必填、取值范围、重复键 |

**解决的问题**：LLM 手写 form-urlencoded 请求体时，参数拼接与类型转换极易出错 ——
漏转义、把 int 写成 string、数组忘记加 `[]`。交给工具更稳妥：

```python
# 手写容易出错
"status=active&page=2&sort=created_at&sort=-id"

# 交给工具：数组自动重复键、特殊字符自动转义
d.dispatch("formcodec_encode", {
    "data": {"status": "active", "page": 2,
             "sort": ["created_at", "-id"], "q": "a&b=c"}
})
# status=active&page=2&sort=created_at&sort=-id&q=a%26b%3Dc
```

提交前自检：

```python
d.dispatch("formcodec_validate_pairs", {
    "pairs": [{"key": "status", "value": "x"}],
    "required_fields": ["status"],
    "allowed_values": {"status": ["active", "closed"]},
})
# {"valid": false, "issues": [{"code": "invalid_value", ...}]}
```

## 实现中的坑

**`for k, v in pairs` 解包 dict** —— 早期版本的取值校验写成这样，
但 `pairs` 的元素是 `{"key":..., "value":...}`，解包拿到的是 dict 的两个字符串键
（`"key"` / `"value"`），永远匹配不上目标字段名，导致校验静默失效。
必须按 `pair["key"]` 取值。

**可空嵌套字段** —— `home: Address | None` 在字段缺失时会走进
"解一个空对象"的分支而报错。空值应直接返回 `None`。

**重复键不能在反扁平化中丢失** —— `tags=a&tags=b` 与 `a[0].name=x` 混在一起时，
整体反扁平化会把 `tags` 压成最后一个值。正确做法是**按字段局部处理**嵌套。

## 目录结构

```
formcodec/
├── formcodec/
│   ├── __init__.py     # 公开 API
│   ├── path.py         # 键路径解析与构造
│   ├── codec.py        # 类型转换与 FieldSpec
│   ├── core.py         # 双向编解码主体
│   ├── agent.py        # agent 工具注册（5 个）
│   └── cli.py          # 三个子命令
├── tests/
│   └── test_formcodec.py  # 84 个测试
├── pyproject.toml
└── README.md
```

## 测试

```bash
pip install -e ".[dev]"
pytest -v
```

## 依赖

| 类型 | 依赖 |
|---|---|
| 运行时 | **无**（仅标准库） |
| 开发 | pytest, pytest-cov, mypy, ruff |
| 可选 | agent-core（启用 agent 工具注册） |

要求 Python ≥ 3.11。

## 许可

MIT
