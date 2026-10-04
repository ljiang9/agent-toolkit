"""工具分发与执行。

对应 OpenAI 文档强调的几条硬性实践：
- 「模型一次可能返回零个、一个或多个调用，要假设有多个」→ dispatch_many
- 「未知函数要显式报错，不要静默忽略」→ 白名单分发
- 「捕获执行异常，把简洁错误返回给模型，同时继续处理同一批里的其他调用」
- 「回传结果必须用完全匹配的 call_id / tool_call_id」
"""

from __future__ import annotations

import inspect
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from .registry import ToolRegistry
from .runtime import ContextManager, RetryPolicy
from .spec import ToolResult, ToolSpec
from .validation import (
    ExecutionError,
    ValidationError,
    coerce_defaults,
    parse_arguments,
    validate_params,
)

logger = logging.getLogger(__name__)


class Dispatcher:
    """把 LLM 的 tool_use 请求分发到注册表里的实现。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        retry_policy: RetryPolicy | None = None,
        context_manager: ContextManager | None = None,
        max_workers: int = 4,
    ) -> None:
        self.registry = registry
        self.retry_policy = retry_policy or RetryPolicy()
        self.context_manager = context_manager or ContextManager()
        self.max_workers = max_workers

    # ---------- 单个调用 ----------

    def dispatch(self, name: str, arguments: Any) -> ToolResult:
        """执行一次工具调用，永不抛异常——错误一律包进 ToolResult。

        这样上层 agent loop 拿到的永远是结构一致的结果，不需要到处 try/except。
        """
        # 1) 解析 arguments（可能是 JSON 字符串）
        try:
            args = parse_arguments(arguments)
        except ValidationError as exc:
            return ToolResult.failure(f"参数解析失败：{exc}")

        # 2) 未知工具显式报错 —— 白名单分发
        if not self.registry.has(name):
            return ToolResult.failure(
                f"未知工具 {name!r}。可用工具：{self.registry.names()}"
            )
        spec = self.registry.get(name)

        # 3) 默认值填充 + schema 校验 + 默认值填充
        #    顺序很关键：带 default 的字段必须先补上，否则 strict schema 的
        #    required 检查会把「有默认值但调用方没传」的合法用法判为非法。
        try:
            args = coerce_defaults(args, spec.parameters)
            validate_params(args, spec.parameters)
        except ValidationError as exc:
            field = f"（字段 {exc.field}）" if exc.field else ""
            return ToolResult.failure(f"参数校验失败{field}：{exc}")

        # 4) 执行（带重试），异常统一规整
        #    default_retryable 传 spec.retryable：工具声明可重试时，
        #    其内部抛出的普通异常也纳入重试。
        def _invoke() -> Any:
            return self._invoke(spec, args)

        try:
            if spec.timeout is not None:
                data = self.retry_policy.run(
                    lambda: self._call_with_timeout(spec, args),
                    retryable=spec.retryable,
                    default_retryable=spec.retryable,
                )
            else:
                data = self.retry_policy.run(
                    _invoke, retryable=spec.retryable, default_retryable=spec.retryable
                )
        except ValidationError as exc:
            return ToolResult.failure(f"参数错误：{exc}")
        except ExecutionError as exc:
            return ToolResult.failure(f"执行失败：{exc}")
        except TypeError as exc:
            # 入参签名不匹配，多半是 schema 与实现脱节了
            return ToolResult.failure(f"调用签名不匹配：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具 %s 执行异常", name)
            logger.debug("%s", traceback.format_exc())
            return ToolResult.failure(f"内部错误：{type(exc).__name__}: {exc}")

        result = data if isinstance(data, ToolResult) else ToolResult.success(data)

        # 5) 上下文预算控制
        return self.context_manager.process(result, tool_name=name)

    def _invoke(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        return spec.handler(**args)

    def _call_with_timeout(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        """在独立线程里跑带超时的调用。

        Python 无法真正杀死卡住的线程，因此这里超时后放弃等待，
        但把超时标记为可重试错误，让上层知道这次调用状态未知。
        """
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self._invoke, spec, args)
            try:
                return future.result(timeout=spec.timeout)
            except TimeoutError:
                raise ExecutionError(
                    f"工具 {spec.name} 执行超时（>{spec.timeout}s）", retryable=True
                ) from None
        finally:
            pool.shutdown(wait=False)

    # ---------- 批量并行 ----------

    def dispatch_many(
        self,
        calls: Sequence[tuple[str, Any]],
        *,
        parallel: bool = True,
    ) -> list[ToolResult]:
        """执行一批调用，返回与输入等长的结果列表。

        `calls` 是 (tool_name, arguments) 序列，对应模型一次返回的多个
        function_call。结果顺序严格对齐输入，调用方按 call_id 逐个回传即可。
        """
        if not calls:
            return []
        if not parallel or len(calls) == 1:
            return [self.dispatch(name, args) for name, args in calls]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(self.dispatch, n, a) for n, a in calls]
            # 同批中某个调用失败不影响其它调用，逐个取结果
            return [f.result() for f in futures]

    # ---------- 与 API 层对接 ----------

    def openai_tool_list(self) -> list[dict[str, Any]]:
        return [s.to_openai() for s in self.registry.specs()]

    def anthropic_tool_list(self) -> list[dict[str, Any]]:
        return [s.to_anthropic() for s in self.registry.specs()]

    def mcp_tool_list(self) -> list[dict[str, Any]]:
        return [s.to_mcp() for s in self.registry.specs()]
