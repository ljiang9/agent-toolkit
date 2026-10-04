"""agent-core —— 可扩展的 AI agent 工具注册与调度层。

对齐两份官方文档描述的 agent 内部工作范式：

Anthropic《Building Effective Agents》
    https://www.anthropic.com/engineering/building-effective-agents

OpenAI Function Calling
    https://developers.openai.com/api/docs/guides/function-calling

能力映射：
    工具调用/function calling .... ToolSpec + Dispatcher + registry
    任务规划与分步执行 .......... Step + Orchestrator
    上下文与记忆管理 ............ ContextManager + MemoryStore + TaskState
    结果校验与重试循环 .......... validate_params + RetryPolicy
    可扩展的工具注册机制 ........ ToolRegistry + entry-point 插件协议

典型用法：

    from agent_core import ToolRegistry, Dispatcher

    registry = ToolRegistry()          # 1) 建注册表
    registry.register_plugin(register) # 2) 注册工具（函数接收 registry）
    dispatcher = Dispatcher(registry)  # 3) 建调度器

    result = dispatcher.dispatch("urlkit_parse", {"url": "https://a.com/p?x=1"})
    print(result.to_model())
"""

from __future__ import annotations

from .dispatcher import Dispatcher
from .memory import MemoryEntry, MemoryStore, TaskState
from .planner import (
    ExecutionErrorStep,
    Orchestrator,
    Step,
    StepResult,
    ValidationErrorStep,
    render_chat_completion_results,
    render_function_call_results,
    tools_prompt,
)
from .registry import ToolProvider, ToolRegistry, call, discover
from .runtime import ContextManager, RetryPolicy
from .spec import ToolResult, ToolSpec, obj_schema
from .validation import (
    ExecutionError,
    ValidationError,
    parse_arguments,
    validate_against_schema,
    validate_params,
)

__version__ = "0.1.0"

__all__ = [
    "ContextManager",
    "Dispatcher",
    "ExecutionError",
    "ExecutionErrorStep",
    "MemoryEntry",
    "MemoryStore",
    "Orchestrator",
    "RetryPolicy",
    "Step",
    "StepResult",
    "TaskState",
    "ToolProvider",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ValidationError",
    "ValidationErrorStep",
    "call",
    "discover",
    "obj_schema",
    "parse_arguments",
    "render_chat_completion_results",
    "render_function_call_results",
    "tools_prompt",
    "validate_against_schema",
    "validate_params",
    "__version__",
]
