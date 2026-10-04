"""agent-core 与五个项目的集成测试。

验证「新增项目只需实现 register(registry)，核心零改动即可调度」这条契约。
"""

from __future__ import annotations

import pytest

from agent_core import (
    Dispatcher,
    Orchestrator,
    RetryPolicy,
    Step,
    TaskState,
    ToolRegistry,
    render_function_call_results,
)

# 五个项目各自独立安装，测试用 try/except 跳过未安装的
INSTALLED: dict[str, bool] = {}


def _load(name: str, module: str):
    try:
        mod = __import__(module, fromlist=["register"])
        INSTALLED[name] = True
        return mod.register
    except ImportError:  # pragma: no cover - 取决于安装情况
        INSTALLED[name] = False
        pytest.skip(f"{name} 未安装")


PROJECTS = [
    ("urlkit", "urlkit.agent"),
    ("snakevision", "snakevision.agent"),
    ("datahacks", "datahacks.agent"),
    ("formcodec", "formcodec.agent"),
    ("taskrun", "taskrun.agent"),
]


@pytest.fixture(scope="module")
def full_registry() -> ToolRegistry:
    """加载全部已安装项目的工具。"""
    reg = ToolRegistry()
    for name, module in PROJECTS:
        try:
            mod = __import__(module, fromlist=["register"])
        except ImportError:  # pragma: no cover
            continue
        mod.register(reg)
    return reg


# ---------- 统一注册 ----------


def test_all_projects_register(full_registry: ToolRegistry) -> None:
    names = full_registry.names()
    assert len(names) >= 30, f"五个项目应注册 30+ 工具，实际 {len(names)}"
    # 每个项目的前缀都应出现
    for prefix in ("urlkit", "snakevision", "data", "formcodec", "taskrun"):
        assert any(n.startswith(prefix) for n in names), f"缺少 {prefix} 的工具"


def test_no_name_collision(full_registry: ToolRegistry) -> None:
    """工具名全局唯一，否则会互相覆盖。"""
    names = full_registry.names()
    assert len(names) == len(set(names))


def test_every_tool_schema_is_strict(full_registry: ToolRegistry) -> None:
    """所有工具的 schema 都必须满足 strict 模式的两条硬规则。

    这是 agent 能否正常工作的前提——任一工具不合规，
    OpenAI 会在请求阶段直接拒收整批调用。
    """
    offenders = []
    for spec in full_registry.specs():
        s = spec.parameters
        if s.get("additionalProperties") is not False:
            offenders.append(f"{spec.name}: additionalProperties != false")
        if set(s.get("required", [])) != set(s.get("properties", {})):
            offenders.append(f"{spec.name}: required 与 properties 不一致")
    assert not offenders, "不合规的工具：\n" + "\n".join(offenders)


def test_every_tool_has_description(full_registry: ToolRegistry) -> None:
    """description 太短会让 LLM 选错工具，这是官方明确强调的点。"""
    for spec in full_registry.specs():
        assert len(spec.description) >= 20, f"{spec.name} 的描述过短"


def test_every_tool_roundtrips_to_api_formats(full_registry: ToolRegistry) -> None:
    """同一注册表能导出成各家 API 格式。"""
    d = Dispatcher(full_registry)
    oai = d.openai_tool_list()
    assert all(t["strict"] is True for t in oai)
    assert all("input_schema" in t for t in d.anthropic_tool_list())
    assert all("inputSchema" in t for t in d.mcp_tool_list())


def test_plugin_discovery_finds_installed_projects() -> None:
    """entry-point 协议应能自动发现已安装的项目。"""
    from agent_core import discover

    reg = discover()
    if len(reg) == 0:  # pragma: no cover - 取决于安装方式
        pytest.skip("没有通过 entry-point 安装任何项目")
    assert len(reg) >= 30


# ---------- 跨项目协作 ----------


def test_cross_project_pipeline_urlkit_to_datahacks(full_registry: ToolRegistry) -> None:
    """urlkit 提取 host -> datahacks 统计长度分布，验证工具能串起来。"""
    d = Dispatcher(full_registry)
    urls = [
        "https://example.com/a", "https://example.com/bb",
        "https://example.org/ccc", "https://example.org/dddd",
    ]
    hosts = []
    for u in urls:
        r = d.dispatch("urlkit_get_component", {"url": u, "component": "host"})
        assert r.ok
        hosts.append(r.data["value"])

    stats = d.dispatch("data_quantiles", {"values": [float(len(h)) for h in hosts]})
    assert stats.ok
    assert "P50" in stats.data["quantiles"]


def test_cross_project_taskrun_checks_datahacks(full_registry: ToolRegistry) -> None:
    """urlkit 算出指纹 -> taskrun 真的跑一条命令把它打印出来并校验。

    跨项目协作的真实场景：一个工具算出结论，另一个工具去验证它确实成立。
    """
    d = Dispatcher(full_registry)
    fp = d.dispatch("urlkit_fingerprint", {"url": "https://e.com/?b=2&a=1#top"})
    assert fp.ok
    value = fp.data["fingerprint"]

    # 指纹含 ? 与 & 交给 shell 会被解释，必须加引号
    chk = d.dispatch("taskrun_check", {
        "command": f"echo '{value}'",
        "expect_stdout_contains": "a=1&b=2",
    })
    assert chk.ok
    assert chk.data["passed"] is True, chk.data["problems"]


def test_orchestrator_chains_tools(full_registry: ToolRegistry) -> None:
    """Orchestrator 按计划分步调用不同项目的工具。"""
    d = Dispatcher(full_registry)
    plan = [
        Step(name="parse", instruction="解析 URL",
             tools=["urlkit_parse"],
             inputs={"tool": "urlkit_parse",
                     "arguments": {"url": "https://a.com/p?x=1"}}),
        Step(name="normalize", instruction="归一化",
             tools=["urlkit_normalize"],
             inputs={"tool": "urlkit_normalize",
                     "arguments": {"url": "https://A.com:443/p?x=1"}}),
    ]
    results = Orchestrator(d).run(plan)
    assert all(r.ok for r in results)
    assert results[0].output["host"] == "a.com"
    assert results[1].output["normalized"] == "https://a.com/p?x=1"


def test_orchestrator_step_tool_allowlist_enforced(full_registry: ToolRegistry) -> None:
    """步骤声明了 tools 白名单后，越权调用应被拒。"""
    d = Dispatcher(full_registry)
    plan = [
        Step(name="s", instruction="", tools=["urlkit_parse"],
             inputs={"tool": "data_histogram", "arguments": {"values": [1, 2]}}),
    ]
    results = Orchestrator(d).run(plan)
    assert results[0].ok is False


def test_batch_dispatch_across_projects(full_registry: ToolRegistry) -> None:
    """一次响应里的多个调用可跨项目并行执行。"""
    d = Dispatcher(full_registry)
    calls = [
        ("urlkit_normalize", {"url": "https://A.com/x"}),
        ("data_percentile", {"values": [1, 2, 3, 4], "percentile": 50}),
        ("snakevision_plan_path", {
            "width": 8, "height": 8,
            "start": {"x": 0, "y": 0}, "goal": {"x": 2, "y": 0},
        }),
    ]
    results = d.dispatch_many(calls)
    assert len(results) == 3
    assert all(r.ok for r in results)
    assert results[0].data["normalized"] == "https://a.com/x"
    assert results[2].data["length"] == 2


def test_batch_failure_isolated(full_registry: ToolRegistry) -> None:
    """同批中某个调用失败不应影响其它调用。"""
    d = Dispatcher(full_registry)
    calls = [
        ("urlkit_normalize", {"url": "https://a.com/x"}),
        ("nonexistent_tool", {}),
        ("data_percentile", {"values": [1, 2, 3], "percentile": 50}),
    ]
    results = d.dispatch_many(calls)
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[2].ok is True


def test_render_results_with_call_ids(full_registry: ToolRegistry) -> None:
    d = Dispatcher(full_registry)
    calls = [("urlkit_normalize", "call_x1")]
    out = render_function_call_results(calls, d.dispatch_many(calls))
    assert out[0]["call_id"] == "call_x1"
    assert out[0]["type"] == "function_call_output"


# ---------- 校验与重试 ----------


def test_bad_args_across_projects_fail_gracefully(full_registry: ToolRegistry) -> None:
    """各项目的参数校验都应返回结构化错误而非抛异常。"""
    d = Dispatcher(full_registry)
    cases = [
        ("urlkit_parse", {}),
        ("data_histogram", {"values": "not a list"}),
        ("snakevision_step", {"state": {"body": []}}),
        ("formcodec_parse_path", {"path": "a["}),
        ("taskrun_run", {"command": "  "}),
    ]
    for name, args in cases:
        r = d.dispatch(name, args)
        assert r.ok is False, f"{name} 应当拒绝非法参数"


def test_unexpected_exception_is_contained(full_registry: ToolRegistry) -> None:
    """工具内部抛未预期异常时，应被规整为错误结果而非向上传播。"""
    reg = ToolRegistry()

    @reg.tool(name="boom", description="故意抛异常的工具", properties={})
    def boom() -> None:
        raise ZeroDivisionError("unexpected")

    r = Dispatcher(reg).dispatch("boom", {})
    assert r.ok is False
    assert "unexpected" in r.error


def test_task_state_tracks_multi_step_progress(full_registry: ToolRegistry) -> None:
    """任务状态记录多步执行进展，供 human-in-the-loop 检查点使用。"""
    d = Dispatcher(full_registry)
    state = TaskState("统计一批 URL 的 host 分布")
    urls = ["https://a.com/1", "https://b.io/2"]

    for u in urls:
        r = d.dispatch("urlkit_get_component", {"url": u, "component": "host"})
        if r.ok:
            state.mark_done(u, r.data["value"])
        else:
            state.mark_failed(u, r.error)

    assert state.remaining(urls) == []
    assert state.progress == 1.0
    assert "统计" in state.summary()


def test_retry_policy_applies_to_retryable_tools(full_registry: ToolRegistry) -> None:
    """声明为 retryable 的工具失败时应走重试。"""
    reg = ToolRegistry()
    state = {"n": 0}

    @reg.tool(name="flaky", description="前两次失败后成功", properties={},
              retryable=True)
    def flaky() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("暂时失败")
        return "ok"

    d = Dispatcher(reg, retry_policy=RetryPolicy(max_attempts=3, initial_backoff=0.01))
    r = d.dispatch("flaky", {})
    assert r.ok and r.data == "ok"
    assert state["n"] == 3
