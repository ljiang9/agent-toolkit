"""urlkit 的 agent 工具接入层。

把 URL 操作注册为 agent 可调用的工具。遵循 OpenAI function calling 规范：
- 每个工具一个明确的 name / description / JSON Schema
- description 说明「何时用它」，而不只是「它做什么」——这是官方强调的
  「像给初级开发者写 docstring 一样」的要求
- 入参出参保持简单可预测，便于模型正确调用
"""

from __future__ import annotations

from typing import Any

from agent_core import ToolRegistry, ToolResult, ToolSpec

from .core import (
    UrlkitError,
    append_component,
    fingerprint,
    get_component,
    normalize,
    parse_url,
    redirect,
    set_component,
)

PROPS_URL = {
    "url": {
        "type": "string",
        "description": "完整的 URL 字符串，例如 https://example.com/path?key=value",
    }
}


def register(registry: ToolRegistry) -> None:
    """把 urlkit 的能力注册进工具注册表。

    entry-point 插件协议会调用这个函数，实现项目与 agent-core 的解耦。
    """

    @registry.tool(
        name="urlkit_parse",
        description=(
            "解析 URL 为结构化组件（scheme/user/password/host/port/path/query/fragment）。"
            "当需要检查 URL 各部分构成、判断 host 与端口、或提取查询参数时使用。"
            "只做解析，不修改 URL。"
        ),
        properties={
            **PROPS_URL,
            "accept_space": {
                "type": ["boolean", "null"],
                "default": True,
                "description": "是否自动编码未转义的空格，默认 True",
            },
        },
    )
    def urlkit_parse(url: str, accept_space: bool | None = None) -> dict[str, Any]:
        parts = parse_url(url, accept_space=True if accept_space is None else accept_space)
        return parts.to_dict()

    @registry.tool(
        name="urlkit_normalize",
        description=(
            "归一化 URL：统一小写 host、去掉默认端口。用于在比较两个 URL "
            "是否指向同一资源之前先做标准化。当需要判断两个链接是否等价、"
            "或去重链接列表时使用。"
        ),
        properties={
            **PROPS_URL,
            "sort_query": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "是否对查询参数排序，排序后可忽略参数顺序差异",
            },
        },
    )
    def urlkit_normalize(url: str, sort_query: bool | None = None) -> dict[str, Any]:
        return {
            "normalized": normalize(
                url, sort_query=bool(sort_query), strip_default_port=True
            )
        }

    @registry.tool(
        name="urlkit_fingerprint",
        description=(
            "生成 URL 的去重指纹：比 normalize 更激进，会排序并去重查询参数、"
            "去掉 fragment 与默认端口、归一尾部斜杠。当需要把同一资源的多个"
            "链接变体聚成一组（如统计 UV 时不重复计数）时使用。"
        ),
        properties=PROPS_URL,
    )
    def urlkit_fingerprint(url: str) -> dict[str, Any]:
        return {"fingerprint": fingerprint(url)}

    @registry.tool(
        name="urlkit_get_component",
        description=(
            "读取 URL 的单个组件值。component 取值：scheme/user/password/"
            "host/port/path/fragment。未显式指定端口时返回 scheme 的默认端口。"
            "当只需从 URL 中取某一个字段（如提取域名做白名单校验）时使用。"
        ),
        properties={
            **PROPS_URL,
            "component": {
                "type": "string",
                "enum": ["scheme", "user", "password", "host", "hostname", "port",
                         "path", "fragment", "url"],
                "description": "要读取的组件名",
            },
        },
    )
    def urlkit_get_component(url: str, component: str) -> dict[str, Any]:
        return {"component": component, "value": get_component(url, component)}

    @registry.tool(
        name="urlkit_rewrite",
        description=(
            "改写 URL 的单个组件并返回新 URL，不会修改原字符串。"
            "当需要替换域名做镜像跳转、注入或替换某个查询参数时使用。"
            "例如：把 host 换成 example.com，或把 query 的 key 改成 page=2。"
        ),
        properties={
            **PROPS_URL,
            "component": {
                "type": "string",
                "enum": ["scheme", "user", "password", "host", "hostname", "port",
                         "path", "fragment"],
                "description": "要改写的组件名",
            },
            "value": {"type": "string", "description": "新的组件值，传空字符串表示清空"},
        },
    )
    def urlkit_rewrite(url: str, component: str, value: str) -> dict[str, Any]:
        return {"url": set_component(url, component, value)}

    @registry.tool(
        name="urlkit_append_query",
        description=(
            "向 URL 的查询参数中追加一项（保留已有参数）。"
            "当需要给现有链接加上跟踪参数、分页参数或筛选条件时使用。"
        ),
        properties={
            **PROPS_URL,
            "key": {"type": "string", "description": "参数名"},
            "value": {"type": "string", "description": "参数值"},
        },
    )
    def urlkit_append_query(url: str, key: str, value: str) -> dict[str, Any]:
        from .core import parse_url as _p

        parts = _p(url, accept_space=True)
        parts.add_query(key, value)
        return {"url": parts.compose()}

    @registry.tool(
        name="urlkit_filter_query",
        description=(
            "按参数名过滤 URL 查询参数，支持 * 与 ? 通配符（如 utm_* 匹配所有 "
            "utm 开头参数）。当需要清理跟踪参数、提取业务参数、或批量去掉 "
            "无关参数时使用。返回新 URL 与被删除的参数个数。"
        ),
        properties={
            **PROPS_URL,
            "remove_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": '要删除的参数名模式列表，如 ["utm_*", "fbclid"]',
            },
            "sort_query": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "是否对剩余参数排序",
            },
        },
    )
    def urlkit_filter_query(
        url: str, remove_patterns: list[str], sort_query: bool | None = None
    ) -> dict[str, Any]:
        parts = parse_url(url, accept_space=True)
        removed = parts.trim_query(remove_patterns)
        if sort_query:
            parts.sort_query()
        return {"url": parts.compose(), "removed_count": removed}

    @registry.tool(
        name="urlkit_redirect_path",
        description=(
            "把 URL 的路径替换为指定的新路径，其余组件（scheme/host/port/query）"
            "保持不变。当需要构造跳转链接、或把深层路径归一到入口路径时使用。"
        ),
        properties={
            **PROPS_URL,
            "new_path": {
                "type": "string",
                "description": "新的路径，可省略前导斜杠，如 here.html",
            },
        },
    )
    def urlkit_redirect_path(url: str, new_path: str) -> dict[str, Any]:
        return {"url": redirect(url, new_path)}

    @registry.tool(
        name="urlkit_safety_check",
        description=(
            "检查 URL 是否存在安全隐患：非 http(s) 协议（如 javascript:、file:、data:）、"
            "内网回环地址（127.0.0.1/localhost）、云元数据地址（169.254.169.254）、"
            "以及是否携带明文凭据（user:password@）。"
            "当用户提交的链接在访问前需要做安全校验时使用。"
        ),
        properties=PROPS_URL,
    )
    def urlkit_safety_check(url: str) -> dict[str, Any]:
        return _safety_check(url)


def _safety_check(url: str) -> dict[str, Any]:
    """URL 安全检查的实现。"""
    from .safety import check_url_safety

    return check_url_safety(url)


__all__ = ["register"]
