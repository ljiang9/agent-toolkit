# agent-core

可扩展的 AI agent 工具注册与调度层。**运行时零第三方依赖**，只用标准库。

对齐两份官方文档描述的 agent 内部工作范式：

- Anthropic《[Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)》
- OpenAI《[Function Calling](https://developers.openai.com/api/docs/guides/function-calling)》

## 能力映射

| 官方范式 | 本模块落地 |
|---|---|
| 工具调用 / function calling | `ToolSpec`（strict JSON Schema）+ `Dispatcher` |
| 任务规划与分步执行 | `Step` + `Orchestrator`（串行 / 并行 sectioning） |
| 上下文与记忆管理 | `ContextManager` + `MemoryStore` + `TaskState` |
| 结果校验与重试循环 | `validate_params` + `RetryPolicy`（指数退避 + 最大迭代上限） |
| 可扩展的工具注册机制 | `ToolRegistry` + entry-point 插件协议 |

## 安装

```bash
pip install -e agent-core          # 本地开发
pip install -e "agent-core[dev]"   # 带测试与类型检查工具
```

要求 Python 3.11+，无运行时依赖。

## 快速开始

```python
from agent_core import ToolRegistry, Dispatcher, ToolSpec

registry = ToolRegistry()

@registry.tool(
    name="add",
    description="两数求和。参数为整数。",
    properties={"a": {"type": "integer"}, "b": {"type": "integer"}},
)
def add(a: int, b: int) -> int:
    return a + b

dispatcher = Dispatcher(registry)
print(dispatcher.dispatch("add", {"a": 1, "b": 2}).to_model())
# {"ok": true, "data": 3}
```

## 导出为 API 格式

同一个注册表可以导出成各家 API 的工具清单格式：

```python
dispatcher.anthropic_tool_list()   # Anthropic Messages API
dispatcher.openai_tool_list()      # OpenAI Responses / Chat Completions
dispatcher.mcp_tool_list()         # MCP tools/list
```

## strict schema 的两条硬规则

`strict: true` 时不满足以下条件，API 会直接拒收请求：

```python
from agent_core import obj_schema

# ✓ 正确：additionalProperties 必须为 false，所有字段进 required
obj_schema({"path": {"type": "string"}})

# 可选字段用 nullable，而不是从 required 移除
obj_schema({
    "limit":  {"type": ["integer", "null"], "default": 50},
    "cursor": {"type": ["string", "null"]},
})
```

## 处理模型返回的调用

```python
from agent_core import Dispatcher, render_function_call_results

dispatcher = Dispatcher(registry)

# 模型一次可能返回零个、一个或多个调用 —— 始终假设有多个
calls = [("add", "call_abc123")]
results = dispatcher.dispatch_many(calls)

# 回传时 call_id 必须与原始调用完全一致
messages = render_function_call_results(calls, results)
```

## 插件注册

各项目在 `pyproject.toml` 声明入口点，`agent-core` 自动发现，**项目之间零硬依赖**：

```toml
[project.entry-points.agent_tools]
urlkit = "urlkit.agent:register"
```

```python
def register(registry):
    registry.add(ToolSpec(name="urlkit_parse", description="...", ...))
```

```python
from agent_core import discover
registry = discover()   # 自动加载所有已安装插件
```

## 大结果处理

工具输出超过 32KB 会自动落盘，结果里回传引用路径而非全文，防止撑爆上下文：

```json
{
  "ok": true,
  "data": "…",
  "artifact_path": "/tmp/agent_artifacts/data_frequency_1.json",
  "note": "结果过大已落盘，请用 read_artifact 工具按路径读取"
}
```

## 错误语义

`dispatch()` **永不抛异常**，所有错误都包进 `ToolResult`：

- 参数非法 → `ok: false`，不重试（重试没有意义，直接让 LLM 改参数）
- 瞬时故障（网络等）→ `spec.retryable = True` 时走指数退避重试
- 达到最大尝试次数 → 触发 `on_exhausted` 回调，作为 human-in-the-loop 切入点

## 许可

MIT
