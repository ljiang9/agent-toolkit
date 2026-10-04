# urlkit

URL 结构化编解码工具。把 URL 拆成八个组件，按组件读写，再重新组装。

复刻自 curl 官方的 [trurl](https://github.com/curl/trurl)，并补充了**归一化**、
**去重指纹**、**安全检查**三项 agent 场景必需的能力。零第三方依赖。

## 快速开始

```bash
pip install -e .
urlkit https://curl.se --set host=example.com
# https://example.com/
```

## 八个组件

| 组件 | 说明 | 读取示例 |
|---|---|---|
| `scheme` | 协议 | `curl.se` → `https` |
| `user` / `password` | 用户名 / 密码 | |
| `host` | 主机名（已剥离端口） | `https://a.com:8080/x` → `a.com` |
| `port` | 端口（未显式指定时返回 scheme 默认值） | `https://a.com/x` → `443` |
| `path` | 路径（点号段已消解） | `/we/../are.html` → `/are.html` |
| `query` | 查询参数（**保留顺序与重复**） | |
| `fragment` | 片段 | |

## 命令行用法

```bash
# 改写组件（--set 可重复，按顺序施加）
urlkit https://curl.se --set host=example.com
urlkit https://curl.se/we/../are.html --set port=8080
# https://curl.se:8080/are.html

# 读取组件（打印裸值，便于管道）
urlkit https://curl.se/we/are.html --get path
# /we/are.html

# 追加（不替换已有内容）
urlkit https://curl.se/hello --append path=you
# https://curl.se/hello/you
urlkit "https://curl.se?name=hello" --append query=search=string
# https://curl.se/?name=hello&search=string

# 重定向（相对路径基于当前目录解析，对齐 trurl）
urlkit https://curl.se/we/are.html --redirect here.html
# https://curl.se/we/here.html

# 清理查询参数（支持 * 与 ? 通配）
urlkit "https://curl.se?search=hey&utm_source=tracker" --qtrim "utm_*"
# https://curl.se/?search=hey

# 参数排序
urlkit "https://example.com?b=a&c=b&a=c" --sort-query
# https://example.com/?a=c&b=a&c=b
```

### 子命令形式

```bash
urlkit parse       https://curl.se/we/are.html   # 结构化组件
urlkit normalize   https://Example.COM:443/a     # 归一化
urlkit fingerprint "https://e.com/?b=2&a=1#top"  # 去重指纹
urlkit check       http://127.0.0.1/            # 安全检查
```

> 子命令须放在 URL 之前：`urlkit parse <url>`。URL 是位置参数，若先给 URL
> 则 argparse 无法判断它是子命令名还是 URL。

### 批量处理

```bash
# 从 stdin 流式处理，自动跳过空行与 # 注释行
cat urls.txt | urlkit batch --qtrim "utm_*"

# 从文件读取
urlkit --url-file urls.txt --get host
```

## 作为库使用

```python
from urlkit import parse_url, fingerprint, normalize, check_url_safety

p = parse_url("https://user:pw@curl.se:8080/we/are.html?a=1&b=2#frag")
p.host          # 'curl.se'  —— 端口已剥离
p.port          # 8080
p.get_query("a")  # ['1']   —— 重复 key 全部返回

p.set_query("page", "2")   # 替换同名项
p.add_query("ref", "x")    # 追加
p.trim_query(["utm_*"])    # 通配删除
p.sort_query()             # 排序（去重指纹的前提）

p.compose()                # 重组回 URL

# 归一化：判断两个 URL 是否等价
normalize("https://Example.COM:443/a") == normalize("https://example.com/a")  # True

# 去重指纹：把同一资源的多个变体聚成一组
fingerprint("https://e.com/?b=2&a=1#top")  # 'https://e.com/?a=1&b=2'
```

## 三项超出 trurl 的能力

### 1. 归一化与去重指纹

```python
normalize("https://Example.COM:443/a?b=1")     # 'https://example.com/a?b=1'
fingerprint("https://e.com/a/")                # 'https://e.com/a'
```

`fingerprint` 比 `normalize` 更激进：排序并去重 query、去掉 fragment、
去默认端口、归一尾部斜杠。做链接去重 / UV 统计时用它。

```python
variants = [
    "https://e.com/a?x=1&y=2",
    "https://e.com/a?y=2&x=1",   # 参数顺序不同
    "https://E.com/a?x=1&y=2#f", # 大小写 + fragment 不同
]
len({fingerprint(v) for v in variants})   # 1 —— 正确聚为一组
```

### 2. 安全检查

访问用户提供的 URL 前的第一道防线，覆盖 prompt injection 与 SSRF：

```python
from urlkit import check_url_safety

check_url_safety("javascript:alert(1)"))
# safe=False, code=scheme_not_allowed

check_url_safety("http://169.254.169.254/latest/meta-data/")
# safe=False, code=cloud_metadata   ← 云元数据，可泄露凭据

check_url_safety("http://127.0.0.1:8080/admin")
# safe=False, code=private_ip      ← SSRF

check_url_safety("https://user:pass@example.com/")
# safe=True，但有 warning: inline_credentials  ← 日志会泄露凭据

check_url_safety("http://")   # safe=False, code=missing_host
```

检查项：协议白名单、云元数据地址、内网/回环/链路本地 IP、明文凭据、非标准端口。

### 3. 严格模式

默认**拒绝**含未编码空格的 URL —— 那通常意味着上游拼接有 bug，静默修复会
掩盖问题。需要时显式开启：

```python
parse_url("https://a.com/this has space/")                        # 抛 UrlkitError
parse_url("https://a.com/this has space/", accept_space=True)     # 空格编码为 %20
```

## 作为 agent 工具

安装 `agent-core` 后自动注册 9 个工具：

```python
from agent_core import ToolRegistry, Dispatcher
from urlkit.agent import register

reg = ToolRegistry()
register(reg)
d = Dispatcher(reg)

d.dispatch("urlkit_fingerprint", {"url": "https://e.com/?b=2&a=1#top"})
# {"ok": true, "data": {"fingerprint": "https://e.com/?a=1&b=2"}}
```

| 工具 | 用途 |
|---|---|
| `urlkit_parse` | 拆成结构化组件 |
| `urlkit_normalize` | 归一化，判断链接等价 |
| `urlkit_fingerprint` | 去重指纹，聚合同一资源 |
| `urlkit_get_component` | 取单个组件 |
| `urlkit_rewrite` | 改写单个组件 |
| `urlkit_append_query` | 追加查询参数 |
| `urlkit_filter_query` | 按通配符清理参数 |
| `urlkit_redirect_path` | 重定向路径 |
| `urlkit_safety_check` | 安全检查 |

也可以让 `agent-core` 通过 entry-point 协议自动发现（安装后即生效）：

```python
from agent_core import discover
registry = discover()   # 自动加载已安装的 agent 工具插件
```

## 设计取舍

- **query 保留顺序与重复**：`?a=1&a=2` 与 `?a=2&a=1` 语义可能不同，
  不能像多数库那样直接丢进 dict。需要去重时显式调 `dedup_query()`。
- **端口区分「未指定」与「显式默认」**：归一化时才能正确判断能否省略。
- **userinfo 用 `quote()` 编码**：产出大写 `%3A`，trurl 用小写 `%3a`，
  两者语义等价（百分号编码大小写不敏感），保证解析往返一致。
- **空格默认报错**：宁可暴露问题也不静默修复。

## 目录结构

```
urlkit/
├── urlkit/
│   ├── __init__.py     # 公开 API
│   ├── core.py         # 解析 / 组装 / 归一化 / 指纹 / 通配匹配
│   ├── safety.py       # 安全检查
│   ├── agent.py        # agent 工具注册（9 个工具）
│   └── cli.py          # 命令行接口
├── tests/
│   └── test_urlkit.py  # 72 个测试
├── pyproject.toml
└── README.md
```

## 测试

```bash
pip install -e ".[dev]"
pytest -v
```

测试用例直接对齐 trurl 官方 README 中的行为契约（`--set` / `--get` /
`--append` / `--redirect` / `--qtrim` / `--sort-query` / `--default-port`），
以及 RFC 3986 的点号段消解规则。

## 依赖

| 类型 | 依赖 |
|---|---|
| 运行时 | **无**（仅标准库） |
| 开发 | pytest, pytest-cov, mypy, ruff |
| 可选 | agent-core（启用 agent 工具注册） |

要求 Python ≥ 3.11。

## 许可

MIT
