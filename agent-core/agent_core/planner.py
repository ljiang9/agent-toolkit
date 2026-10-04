"""规划与分步执行。

对应 Anthropic《Building Effective Agents》里的两类模式：

- **Orchestrator-workers**（动态分解）：步骤数无法预先确定时，由 LLM 动态拆解任务、
  分派给 worker、再汇总。适合「要查哪个源、要改哪些文件」这类开放式问题。
- **Prompt chaining / Routing**（预定义编排）：步骤固定时用确定性工作流，
  而不是让 agent 完全自主。官方建议是能从简单可评估的方案开始就别上自主 agent。

这里两种都给了：Plan 用静态步骤列表表达 chaining，
Worker 阶段允许每个步骤内部再动态分派子任务，表达 orchestrator-workers。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .dispatcher import Dispatcher
from .spec import ToolResult

logger = logging.getLogger(__name__)


@dataclass
class Step:
    """一个执行步骤。

    `tools` 声明本步允许调用的工具子集——对应 OpenAI 的 allowed_tools 机制：
    保留完整 tools 定义、限制本轮可选范围，既收敛行为又利于 prompt caching。
    """

    name: str
    instruction: str
    # 本步允许调用的工具名；None 表示不限制
    tools: Sequence[str] | None = None
    # 传给本步的输入；可用上一步输出插值
    inputs: dict[str, Any] = field(default_factory=dict)
    # 拿到输出后可选的后处理
    postprocess: Callable[[Any], Any] | None = None


@dataclass
class StepResult:
    """单步执行结果，供规划层做分支判断。"""

    step: str
    ok: bool
    output: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "ok": self.ok, "output": self.output, "error": self.error}


class Orchestrator:
    """按计划分步执行，支持串行与并行。

    执行结果是「确定性的」：给定相同计划与工具响应，输出可复现。
    LLM 只负责填计划，不直接决定控制流——这样每一步都可审计、可回放。
    """

    def __init__(self, dispatcher: Dispatcher, *, continue_on_error: bool = False) -> None:
        self.dispatcher = dispatcher
        self.continue_on_error = continue_on_error

    def run(self, plan: Sequence[Step]) -> list[StepResult]:
        """串行执行。后续步骤可以读到前序结果（通过 ``_previous`` 占位符）。"""
        results: list[StepResult] = []
        for step in plan:
            resolved = self._resolve_inputs(step, results)
            try:
                output = self._run_step(step, resolved)
                results.append(StepResult(step=step.name, ok=True, output=output))
            except Exception as exc:  # noqa: BLE001
                logger.warning("步骤 %s 失败：%s", step.name, exc)
                results.append(StepResult(step=step.name, ok=False, error=str(exc)))
                if not self.continue_on_error:
                    break
        return results

    def run_parallel(self, plan: Sequence[Step]) -> list[StepResult]:
        """并行执行无依赖的步骤（sectioning 并行模式）。

        注意：并行步骤之间不能互相引用输出，因为它们同时开始。
        """
        from concurrent.futures import ThreadPoolExecutor

        if not plan:
            return []

        def _one(step: Step) -> StepResult:
            try:
                return StepResult(step=step.name, ok=True, output=self._run_step(step, step.inputs))
            except Exception as exc:  # noqa: BLE001
                return StepResult(step=step.name, ok=False, error=str(exc))

        with ThreadPoolExecutor(max_workers=self.dispatcher.max_workers) as pool:
            return list(pool.map(_one, plan))

    def _run_step(self, step: Step, inputs: dict[str, Any]) -> Any:
        """执行一步。

        约定的调用约定：步骤的 handler 约定为 ``fn(dispatcher, inputs) -> Any``，
        这样每步都能自己决定调哪个工具、怎么组合，而编排层保持通用。
        """
        if step.postprocess is not None:
            return step.postprocess(inputs)

        # 没有 postprocess 时，默认按 inputs 里的 tool/arguments 直接调一次。
        # 若 inputs 中不含 tool，则视为「纯数据步骤」——把 inputs 里每个
        # $from 引用解析出的值原样返回，而不是把整个 dict 当结果。
        tool_name = inputs.get("tool")
        arguments = inputs.get("arguments", {})
        if tool_name:
            allowed = None if step.tools is None else set(step.tools)
            if allowed is not None and tool_name not in allowed:
                raise ValidationErrorStep(
                    f"步骤 {step.name!r} 不允许调用工具 {tool_name!r}，"
                    f"允许的是 {sorted(allowed)}"
                )
            result: ToolResult = self.dispatcher.dispatch(tool_name, arguments)
            if not result.ok:
                raise ExecutionErrorStep(result.error or "工具执行失败")
            return result.data

        # 纯数据步骤：单个引用直接取其值（步骤之间可直接串起来），
        # 多个引用则返回以键为名的 dict。
        if "arguments" in inputs:
            return arguments
        if len(inputs) == 1:
            return next(iter(inputs.values()))
        return inputs

    def _resolve_inputs(self, step: Step, prior: list[StepResult]) -> dict[str, Any]:
        """把 ``{"$from": "step_name"}`` 替换成该步的真实输出。"""
        resolved: dict[str, Any] = {}
        by_name = {r.step: r for r in prior}
        for key, value in step.inputs.items():
            if isinstance(value, dict) and "$from" in value:
                src = by_name.get(value["$from"])
                if src is None:
                    raise ValidationErrorStep(
                        f"步骤 {step.name} 引用了不存在的步骤 {value['$from']!r}"
                    )
                if not src.ok:
                    raise ExecutionErrorStep(
                        f"步骤 {step.name} 依赖的 {src.step} 失败：{src.error}"
                    )
                resolved[key] = src.output
            else:
                resolved[key] = value
        return resolved


class ValidationErrorStep(Exception):
    """步骤层的参数/依赖错误，不重试。"""


class ExecutionErrorStep(Exception):
    """步骤层的执行错误，可按需重试整步。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ---------- 与 LLM 交互的辅助函数 ----------

def tools_prompt(dispatcher: Dispatcher) -> str:
    """把工具清单渲染成给规划器看的提示词片段。"""
    lines = []
    for spec in dispatcher.registry.specs():
        params = ", ".join((spec.parameters.get("properties") or {}).keys())
        lines.append(f"- {spec.name}({params}): {spec.description}")
    return "\n".join(lines)


def render_function_call_results(
    calls: Sequence[tuple[str, str]],
    results: Sequence[ToolResult],
) -> list[dict[str, Any]]:
    """把执行结果转成 OpenAI Responses API 的 function_call_output 列表。

    `calls` 是 (name, call_id) 序列；call_id 必须**原样带回**，
    不能用 function_call 项的 id 代替，否则后续请求会校验失败。
    """
    out: list[dict[str, Any]] = []
    for (name, call_id), result in zip(calls, results):
        out.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result.to_model(),
            }
        )
    return out


def render_chat_completion_results(
    calls: Sequence[tuple[str, str]],
    results: Sequence[ToolResult],
) -> list[dict[str, Any]]:
    """转成 Chat Completions 的 tool 消息列表（用 tool_call_id 而非 call_id）。"""
    return [
        {"role": "tool", "tool_call_id": call_id, "content": result.to_model()}
        for (_name, call_id), result in zip(calls, results)
    ]
