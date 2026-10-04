"""重试策略与上下文管理。

两件事：
1. RetryPolicy —— 对应 Anthropic 讲的 evaluator-optimizer loop 与「结果校验与重试循环」；
   同时遵守官方建议：设置最大迭代次数作为停止条件，防止自主 agent 烧钱。
2. ContextManager —— 对应「上下文与记忆管理」：工具输出可能远超预算，
   必须在进入下一轮前截断/压缩/落盘。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .spec import ToolResult


# ---------------- 重试 ----------------

@dataclass
class RetryPolicy:
    """指数退避重试。

    只对 retryable 的失败重试——参数写错了重试一万次也没用。
    """

    max_attempts: int = 3
    initial_backoff: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff: float = 8.0
    # 达到最大尝试次数仍失败时的回调，用于上报阻塞（human-in-the-loop 切入点）
    on_exhausted: Callable[[str, BaseException | None, int], None] | None = None

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次尝试（从 1 开始）失败后应等待的秒数。"""
        d = self.initial_backoff * (self.backoff_multiplier ** (attempt - 1))
        return min(d, self.max_backoff)

    def run(
        self,
        fn: Callable[[], Any],
        *,
        retryable: bool = True,
        default_retryable: bool | None = None,
    ) -> Any:
        """执行 fn，失败时按指数退避重试。

        重试决策的优先级（这三条合起来才完整）：
        1. 异常自带 ``retryable`` 属性 → 以它为准（最具体，如超时/网络错误）；
        2. 否则用 ``default_retryable``（通常来自 ToolSpec.retryable）；
        3. 都没有 → 不重试。

        第 2 条是关键：工具声明自己「可重试」后，函数内部抛出的普通异常
        （RuntimeError 等）也应纳入重试，而不是因为异常没带标记就静默不重试。
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - 需要统一规整为重试决策
                last_exc = exc
                explicit = getattr(exc, "retryable", None)
                if explicit is not None:
                    is_retryable = bool(explicit)
                elif default_retryable is not None:
                    is_retryable = bool(default_retryable)
                else:
                    is_retryable = retryable
                # 不可重试的错误（如参数非法）立即抛出，别浪费配额
                if not (retryable and is_retryable) or attempt >= self.max_attempts:
                    if self.on_exhausted:
                        self.on_exhausted(getattr(fn, "__name__", "fn"), exc, attempt)
                    raise
                time.sleep(self.delay_for(attempt))
        if last_exc:  # pragma: no cover - 循环必然 raise 或 return
            raise last_exc
        raise RuntimeError("unreachable")


# ---------------- 上下文管理 ----------------

@dataclass
class ContextManager:
    """工具结果进上下文前的预算控制。

    策略：
    - 小结果直接内联；
    - 超阈值先尝试结构化压缩（只保留摘要字段）；
    - 还是超就落盘，回传引用路径 + 说明，让 agent 用 read_artifact 按需读取。

    这直接对应 Anthropic 文档里「模型需要决定什么信息值得保留」的做法。
    """

    max_inline_bytes: int = 32 * 1024
    artifact_dir: str | None = None
    _counter: int = field(default=0, init=False)

    def _dir(self) -> str:
        d = self.artifact_dir or os.path.join(tempfile.gettempdir(), "agent_artifacts")
        os.makedirs(d, exist_ok=True)
        return d

    def process(self, result: ToolResult, *, tool_name: str) -> ToolResult:
        """按体积处理结果，必要时落盘并回填 artifact_path。"""
        if not result.ok or result.data is None:
            return result

        payload = result.to_model()
        if len(payload.encode("utf-8")) <= self.max_inline_bytes:
            return result

        self._counter += 1
        path = os.path.join(self._dir(), f"{tool_name}_{self._counter}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result.data, fh, ensure_ascii=False, indent=2, default=str)

        result.artifact_path = path
        result.meta["original_bytes"] = len(payload.encode("utf-8"))
        logger_msg = f"结果 {len(payload)} 字节超限，已落盘 {path}"
        if not result.meta.get("note"):
            result.meta["note"] = logger_msg
        return result

    @staticmethod
    def summarize(data: Any, *, max_items: int = 20) -> Any:
        """把大列表压成摘要：保留前 N 项与总数。"""
        if isinstance(data, list) and len(data) > max_items:
            return {
                "_truncated": True,
                "_total": len(data),
                "_shown": max_items,
                "_preview": data[:max_items],
            }
        if isinstance(data, dict) and len(data) > max_items:
            keys = list(data)[:max_items]
            return {
                "_truncated": True,
                "_total": len(data),
                "_shown": max_items,
                "_keys": keys,
            }
        return data
