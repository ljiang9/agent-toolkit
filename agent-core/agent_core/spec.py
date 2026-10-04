"""Tool specification primitives.

对齐 OpenAI Function Calling 规范：工具用 `strict: true` 的 JSON Schema 描述，
约束为——每个 object 设 `additionalProperties: false`、所有 properties 必须出现在
`required` 中、可选字段用 nullable 而非移出 required。

参考：
- OpenAI Function Calling: https://developers.openai.com/api/docs/guides/function-calling
- Anthropic Tool Use: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# JSON Schema 原始类型
JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的可调用契约。

    这个对象是「LLM 能看见的东西」与「Python 实现」之间的唯一桥梁。
    描述写得好坏直接决定 LLM 选不选它、用不对参数的概率有多大，
    所以字段说明要像给新人写 docstring 一样清楚（Anthropic 的建议）。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    # 失败时是否值得重试（网络抖动等瞬时错误 True，参数错误等 False）
    retryable: bool = False
    # 单次调用超时（秒），None 表示不限制
    timeout: float | None = 30.0
    # 结果体积上限（字节），超出则落盘并只回传引用路径，防止撑爆上下文
    max_inline_bytes: int = 32 * 1024

    def to_openai(self) -> dict[str, Any]:
        """转成 OpenAI Responses API 的 tools 条目格式。"""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": True,
        }

    def to_anthropic(self) -> dict[str, Any]:
        """转成 Anthropic Messages API 的 tools 条目格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_mcp(self) -> dict[str, Any]:
        """转成 MCP (Model Context Protocol) 的 tools/list 条目格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }


def obj_schema(
    properties: dict[str, Any],
    *,
    required: Iterable[str] | None = None,
) -> dict[str, Any]:
    """构造符合 strict 模式要求的 object schema。

    strict 模式的两条硬性规则（不满足会被 API 直接拒收）：
    1. 每个 object 都要有 additionalProperties: false
    2. properties 里出现的字段必须全部写进 required

    因此「可选字段」要用 `["string", "null"]` 这种 nullable 类型表达，
    而不是从 required 里去掉。

    >>> obj_schema({"path": {"type": "string"}})
    {'type': 'object', 'properties': {'path': {'type': 'string'}}, \
'required': ['path'], 'additionalProperties': False}
    """
    props = dict(properties)
    return {
        "type": "object",
        "properties": props,
        "required": list(required) if required is not None else list(props),
        "additionalProperties": False,
    }


@dataclass
class ToolResult:
    """一次工具调用的结果。

    强制区分成功与失败，而不是靠调用方去看返回值猜——这对应 OpenAI 文档里
    「catch execution exceptions, log details internally, return a concise
    error to the model」的实践。
    """

    ok: bool
    data: Any = None
    error: str | None = None
    # 大结果落盘后的引用路径
    artifact_path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_model(self) -> str:
        """转成回传给 LLM 的字符串（必须是字符串，结构化结果要先序列化）。"""
        import json

        payload: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            payload["data"] = self.data
            if self.artifact_path:
                payload["artifact_path"] = self.artifact_path
                payload["note"] = (
                    f"结果过大已落盘到 {self.artifact_path}，"
                    f"完整内容请用 read_artifact 工具按路径读取"
                )
        else:
            payload["error"] = self.error
        if self.meta:
            payload["meta"] = self.meta
        return json.dumps(payload, ensure_ascii=False, default=str)

    @classmethod
    def success(cls, data: Any, **meta: Any) -> "ToolResult":
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(cls, error: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, error=error, meta=meta)
