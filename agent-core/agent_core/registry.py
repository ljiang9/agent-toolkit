"""可扩展的工具注册表。

新增能力只需实现 `register(registry)`，agent-core 零改动即可调度。
支持两种发现方式：

1. entry-point 插件协议（生产用）——各项目在 pyproject.toml 声明入口点，
   互不产生硬依赖，可以单独安装单独运行：
       [project.entry-points.agent_tools]
       urlkit = "urlkit.agent:register"

2. 显式调用 `registry.register_plugin(module_obj)`（测试/脚本用）
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Protocol

from .spec import ToolResult, ToolSpec, obj_schema

logger = logging.getLogger(__name__)


class ToolProvider(Protocol):
    """任何能往注册表里塞工具的对象。"""

    def tools(self) -> Iterable[ToolSpec]: ...


class ToolRegistry:
    """工具注册表：按名字索引全部可用工具。

    使用方（Dispatcher）只认名字，不关心工具来自哪个项目——这是
    「统一注册」的关键：项目之间彼此不可见，只跟注册表打交道。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        # 记录每个工具的来源项目，便于排查与文档生成
        self._origins: dict[str, str] = {}

    # ---------- 注册 ----------

    def add(self, spec: ToolSpec, *, origin: str = "unknown") -> ToolSpec:
        """注册一个工具。重复名字直接报错，避免静默覆盖。"""
        if spec.name in self._specs:
            raise ValueError(
                f"工具名冲突：{spec.name!r} 已由 {self._origins[spec.name]} 注册，"
                f"请换一个名字"
            )
        self._specs[spec.name] = spec
        self._origins[spec.name] = origin
        logger.debug("已注册工具 %s (origin=%s)", spec.name, origin)
        return spec

    def tool(
        self,
        *,
        name: str,
        description: str,
        properties: dict[str, Any] | None = None,
        required: Iterable[str] | None = None,
        retryable: bool = False,
        timeout: float | None = 30.0,
        max_inline_bytes: int = 32 * 1024,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器：把一个函数注册成工具。

        函数签名里的类型注解会被用来生成 JSON Schema，省得手写。

        >>> reg = ToolRegistry()
        >>> @reg.tool(name="echo", description="回显输入",
        ...           properties={"text": {"type": "string"}})
        ... def echo(text: str) -> str:
        ...     return text
        >>> reg.has("echo")
        True
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            spec = ToolSpec(
                name=name,
                description=description,
                parameters=obj_schema(properties or {}, required=required),
                handler=fn,
                retryable=retryable,
                timeout=timeout,
                max_inline_bytes=max_inline_bytes,
            )
            self.add(spec, origin=f"{fn.__module__}")
            return fn

        return decorator

    def register_provider(self, provider: ToolProvider, *, origin: str = "unknown") -> int:
        """从 provider 对象批量注册，返回注册数量。"""
        specs = list(provider.tools())
        for spec in specs:
            self.add(spec, origin=origin)
        return len(specs)

    def register_plugin(self, fn: Callable[[ToolRegistry], Any], *, origin: str = "unknown") -> int:
        """执行插件的 register(registry) 函数完成注册。"""
        before = len(self._specs)
        fn(self)
        added = len(self._specs) - before
        logger.debug("插件 %s 注册了 %d 个工具", origin, added)
        return added

    # ---------- 查询 ----------

    def get(self, name: str) -> ToolSpec:
        """按名字取工具。未知工具必须显式报错，不能静默忽略。"""
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(
                f"未知工具 {name!r}。可用工具：{sorted(self._specs)}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def origins(self) -> dict[str, str]:
        return dict(self._origins)

    def subset(self, names: Iterable[str]) -> list[ToolSpec]:
        """按给定子集取工具——对应 OpenAI 的 allowed_tools 机制。

        保留完整 tools 定义但限制本轮可选子集，有利于 prompt caching。
        """
        return [self.get(n) for n in names]

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs


def discover(entry_point_group: str = "agent_tools") -> ToolRegistry:
    """通过 entry-point 插件协议发现并加载全部工具。

    未安装任何插件时返回空注册表，不报错——这样 agent-core 可以单独测试。
    """
    registry = ToolRegistry()
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python < 3.8
        return registry

    try:
        eps = entry_points(group=entry_point_group)
    except TypeError:  # pragma: no cover - 老版本 API
        eps = entry_points().get(entry_point_group, [])  # type: ignore[call-arg]

    for ep in eps:
        try:
            fn = ep.load()
            registry.register_plugin(fn, origin=ep.name)
            logger.info("已加载插件 %s", ep.name)
        except Exception as exc:  # 一个插件坏掉不该拖垮整个 agent
            logger.warning("插件 %s 加载失败：%s", ep.name, exc)
    return registry


def call(registry: ToolRegistry, name: str, arguments: dict[str, Any]) -> ToolResult:
    """便捷入口：按名字取工具并执行，返回统一的 ToolResult。"""
    from .dispatcher import Dispatcher

    return Dispatcher(registry).dispatch(name, arguments)
