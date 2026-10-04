# agent-toolkit

五个可复刻的小型项目 + 一套可扩展的 AI agent 工具注册与调度架构。

所有项目**零第三方运行时依赖**，只用 Python 标准库；每个都能独立安装、独立运行，
同时通过 entry-point 插件协议接入 agent 调度层。

## 目录结构

```
agent-toolkit/
├── agent-core/              # agent 运行时（工具注册、调度、校验、重试、记忆）
│   ├── agent_core/
│   │   ├── spec.py          #   ToolSpec（strict JSON Schema）、ToolResult
│   │   ├── registry.py      #   ToolRegistry、entry-point 插件发现
│   │   ├── validation.py    #   参数解析、schema 与业务校验
│   │   ├── runtime.py       #   RetryPolicy、ContextManager
│   │   ├── dispatcher.py    #   Dispatcher（单次/批量、白名单分发）
│   │   ├── planner.py       #   Step、Orchestrator、结果回传格式
│   │   └── memory.py        #   MemoryStore、TaskState
│   └── tests/               # 40 个测试
├── projects/
│   ├── urlkit/              # URL 结构化编解码        72 个测试
│   ├── snakevision/         # BFS/A* 寻路 + 终端渲染  61 个测试
│   ├── datahacks/           # 命令行数据分析工具集    76 个测试
│   ├── formcodec/           # 表单/查询参数双向编解码 84 个测试
│   └── taskrun/             # 进程执行 + 心跳上报     76 个测试
├── tests/                   # 跨项目集成测试 17 个
├── LICENSE
└── README.md
```

## 五个项目

| 项目 | 类型 | 来源 | 一句话 |
|---|---|---|---|
| **urlkit** | 命令行工具 | [curl/trurl](https://github.com/curl/trurl)（3,355★） | URL 拆成八组件按需读写，另加归一化、去重指纹与安全检查 |
| **snakevision** | 创意交互 | [AngelJumbo/sssnake](https://github.com/AngelJumbo/sssnake)（234★） | 会自己玩的贪吃蛇，BFS/A\* + 三级存活策略 + 双缓冲渲染 |
| **datahacks** | 数据处理 | [bitly/data_hacks](https://github.com/bitly/data_hacks)（1,970★） | 纯标准库的直方图/分位数/频次/去重/抽样/哈希工具集 |
| **formcodec** | Web/API 工具 | [go-playground/form](https://github.com/go-playground/form)（930★） | 结构体 ⇄ form-urlencoded 双向编解码，自动类型转换 |
| **taskrun** | 自动化脚本 | [bdd/runitor](https://github.com/bdd/runitor)（357★） | 子进程执行 + 成败判定 + 心跳上报，零依赖 Cron 调度 |

### 能力边界互不相交

| 项目 | 输入输出类型 |
|---|---|
| urlkit | 单个 URL 字符串的结构化编解码 |
| snakevision | 二维网格 + BFS 寻路 + 实时渲染 |
| datahacks | 数值数据表的统计原语计算 |
| formcodec | 键值结构体 ⇄ 表单参数双向映射 |
| taskrun | 进程生命周期管理与成败判定 |

任一项目都无法替代另一项目，也不存在功能包含关系。

## 安装

各项目相互独立，可单独安装：

```bash
pip install -e agent-core          # agent 运行时
pip install -e projects/urlkit     # 或只装你需要的某个
pip install -e projects/datahacks
```

带开发依赖（pytest / mypy / ruff）：

```bash
pip install -e "projects/urlkit[dev]"
```

要求 Python ≥ 3.11。

## 作为独立 CLI 使用

每个项目都有自己的命令行入口，互不依赖：

```bash
urlkit https://curl.se --set host=example.com
urlkit check http://169.254.169.254/          # 拦截云元数据地址

seq 1 100 | datahacks histogram --bins 10
datahacks quantile --pct 95 --file metrics.txt

echo '{"name":"joey","tags":["a","b"]}' | formcodec encode

taskrun --json -- pytest -q
taskrun --slug db-backup --every 2h24m -- /script/backup

snakevision --once --style blocks --show-grid
snakevision --benchmark 500 --try-hard
```

## 作为 agent 工具使用

安装后 `agent-core` 会通过 entry-point 协议自动发现全部工具：

```python
from agent_core import discover, Dispatcher

registry = discover()          # 自动加载所有已安装的项目
dispatcher = Dispatcher(registry)

dispatcher.dispatch("urlkit_fingerprint", {"url": "https://e.com/?b=2&a=1#top"})
# {"ok": true, "data": {"fingerprint": "https://e.com/?a=1&b=2"}}

dispatcher.dispatch("data_percentile", {"values": [10, 20, 30], "percentile": 95})
# {"ok": true, "data": {"percentile": 95, "value": 29.0}}
```

同一注册表可导出成各家 API 的工具清单格式：

```python
dispatcher.anthropic_tool_list()   # Anthropic Messages API
dispatcher.openai_tool_list()      # OpenAI Responses / Chat Completions
dispatcher.mcp_tool_list()         # MCP tools/list
```

### 工具清单（34 个）

| 项目 | 工具 |
|---|---|
| urlkit | `urlkit_parse` `urlkit_normalize` `urlkit_fingerprint` `urlkit_get_component` `urlkit_rewrite` `urlkit_append_query` `urlkit_filter_query` `urlkit_redirect_path` `urlkit_safety_check` |
| snakevision | `snakevision_plan_path` `snakevision_new_game` `snakevision_step` `snakevision_safe_moves` `snakevision_render` |
| datahacks | `data_histogram` `data_quantiles` `data_percentile` `data_describe` `data_frequency` `data_unique` `data_sample` `data_hash` `data_pivot` |
| formcodec | `formcodec_encode` `formcodec_decode` `formcodec_to_json` `formcodec_parse_path` `formcodec_validate_pairs` |
| taskrun | `taskrun_run` `taskrun_run_many` `taskrun_check` `taskrun_heartbeat` `taskrun_schedule_info` `taskrun_split_command` |

## 架构设计

对齐两份官方文档描述的 agent 内部工作范式：

- Anthropic《[Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)》
- OpenAI《[Function Calling](https://developers.openai.com/api/docs/guides/function-calling)》

| 官方范式 | 本仓库落地 |
|---|---|
| 工具调用 / function calling | `ToolSpec`（strict JSON Schema）+ `Dispatcher` |
| 任务规划与分步执行 | `Step` + `Orchestrator`（串行 / 并行 sectioning） |
| 上下文与记忆管理 | `ContextManager` + `MemoryStore` + `TaskState` |
| 结果校验与重试循环 | `validate_params` + `RetryPolicy`（指数退避 + 迭代上限） |
| 可扩展的工具注册机制 | `ToolRegistry` + entry-point 插件协议 |

### 数据流

```
LLM (Claude / OpenAI)
    │  返回零个/一个/多个 tool_use
    ▼
Dispatcher ── 白名单分发（未知工具显式报错）
    │
    ├─ ToolRegistry.get(name)        工具契约 + JSON Schema
    ├─ validate_params               schema 校验 + 业务规则校验
    ├─ 执行（带 RetryPolicy 指数退避）
    └─ ContextManager                超限落盘，回传引用路径
    │
    ▼  ToolResult（永不抛异常）
Orchestrator ── 分步编排 + 依赖解析
    │
    ▼
渲染成 function_call_output（call_id 原样带回）──► LLM
```

### 三条关键设计取舍

**1. `dispatch()` 永不抛异常。** 所有错误包进 `ToolResult`，让 agent loop 拿到
结构一致的结果，不需要到处 try/except。区分「该重试」（`spec.retryable`）与
「该报错让模型改参数」（schema 校验失败）。

**2. 大结果落盘而非塞进上下文。** 超过 32KB 的工具输出自动写入文件，结果里回传
引用路径与说明，让 agent 按需读取。对应官方「模型需要决定什么信息值得保留」的做法。

**3. 新增项目零改动接入。** 实现 `register(registry)` 函数，在 `pyproject.toml`
声明 entry-point 即可。`agent-core` 不需要任何修改，项目之间也彼此不可见。

```toml
[project.entry-points.agent_tools]
yourproject = "yourproject.agent:register"
```

```python
def register(registry):
    registry.add(ToolSpec(name="your_tool", description="...", ...))
```

### strict schema 的两条硬规则

`strict: true` 时不满足以下条件，API 会直接拒收整批请求：

```python
from agent_core import obj_schema

# ✓ additionalProperties 必须为 false，所有字段进 required
obj_schema({"path": {"type": "string"}})

# 可选字段用 nullable，而不是从 required 移除
obj_schema({"limit": {"type": ["integer", "null"], "default": 50}})
```

集成测试会遍历全部 34 个工具校验这两条。

## 测试

```bash
# 全量（426 个测试）
for d in agent-core projects/*; do (cd "$d" && pytest); done
pytest tests/          # 跨项目集成测试

# 单个项目
cd projects/urlkit && pytest -v
```

## 各项目详细文档

- [agent-core](agent-core/README.md)
- [urlkit](projects/urlkit/README.md)
- [snakevision](projects/snakevision/README.md)
- [datahacks](projects/datahacks/README.md)
- [formcodec](projects/formcodec/README.md)
- [taskrun](projects/taskrun/README.md)

## 许可

MIT
