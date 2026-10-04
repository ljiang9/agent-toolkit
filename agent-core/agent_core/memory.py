"""记忆与状态存储。

对应 Anthropic 讲的 memory 三要素与「上下文管理」：
- 短期/会话内状态（当前任务做到哪一步）
- 长期事实（跨会话需要记住的偏好、约定）
- 检索（把相关内容捞回上下文）

这里给一个够用的最小实现：会话内用轻量 JSON 文件持久化，
不引入向量库等重依赖——官方建议也是从简单可评估的方案开始。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass
class MemoryEntry:
    """一条记忆。"""

    key: str
    value: Any
    # 作用域：session（单次会话）/ global（跨会话长期）
    scope: str = "session"
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def age_seconds(self) -> float:
        return time.time() - self.created_at


class MemoryStore:
    """JSON 文件支撑的记忆存储。

    选文件而非数据库的理由：零依赖、可直接查看与编辑、便于测试隔离。
    数据量大时换成 SQLite 只需替换 put/get 两个方法。
    """

    def __init__(self, path: str | None = None, *, namespace: str = "default") -> None:
        self.path = path
        self.namespace = namespace
        self._entries: dict[str, MemoryEntry] = {}
        if path and os.path.exists(path):
            self.load()

    def put(self, key: str, value: Any, *, scope: str = "session", tags: Iterable[str] = ()) -> MemoryEntry:
        entry = MemoryEntry(key=key, value=value, scope=scope, tags=list(tags))
        self._entries[key] = entry
        return entry

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._entries.get(key)
        return default if entry is None else entry.value

    def has(self, key: str) -> bool:
        return key in self._entries

    def delete(self, key: str) -> bool:
        return self._entries.pop(key, None) is not None

    # ---------- 检索 ----------

    def search(self, *, tags: Iterable[str] | None = None, scope: str | None = None,
               limit: int = 10) -> list[MemoryEntry]:
        """按标签与作用域筛选，按新近度排序。"""
        want = set(tags or [])
        hits = [
            e for e in self._entries.values()
            if (scope is None or e.scope == scope)
            and (not want or want & set(e.tags))
        ]
        hits.sort(key=lambda e: e.created_at, reverse=True)
        return hits[:limit]

    def keys(self) -> list[str]:
        return sorted(self._entries)

    # ---------- 持久化 ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "entries": {k: asdict(v) for k, v in self._entries.items()},
        }

    def save(self, path: str | None = None) -> str:
        target = path or self.path
        if not target:
            raise ValueError("未指定存储路径")
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return target

    def load(self, path: str | None = None) -> None:
        target = path or self.path
        if not target or not os.path.exists(target):
            return
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.namespace = raw.get("namespace", self.namespace)
        for key, data in (raw.get("entries") or {}).items():
            self._entries[key] = MemoryEntry(**data)


class TaskState:
    """任务级状态：记录多步执行的进展，防止重复或遗漏工作。

    Anthropic 文档里说，memory 的关键作用是让模型能保持任务状态、
    避免重复或遗漏、根据最新反馈调整后续行动——这就是那个载体。
    """

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.notes: dict[str, Any] = {}

    def mark_done(self, step: str, output: Any = None) -> None:
        if step not in self.completed:
            self.completed.append(step)
        if output is not None:
            self.notes[step] = output

    def mark_failed(self, step: str, error: str) -> None:
        if step not in self.failed:
            self.failed.append(step)
        self.notes[f"{step}::error"] = error

    def is_done(self, step: str) -> bool:
        return step in self.completed

    def remaining(self, all_steps: Iterable[str]) -> list[str]:
        done = set(self.completed)
        return [s for s in all_steps if s not in done]

    @property
    def progress(self) -> float:
        total = len(self.completed) + len(self.failed)
        return len(self.completed) / total if total else 0.0

    def summary(self) -> str:
        """给人看的进展摘要，用于 human-in-the-loop 检查点。"""
        lines = [f"目标：{self.goal}", f"进度：{self.completed}"]
        if self.failed:
            lines.append(f"失败：{self.failed}")
        return "\n".join(lines)
