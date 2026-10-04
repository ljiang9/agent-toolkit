"""agent-core 基础测试。

覆盖官方文档里明确强调的几条行为契约，确保调度层真的按规范工作。
"""

from __future__ import annotations

import json
import os

import pytest

from agent_core import (
    ContextManager,
    Dispatcher,
    MemoryStore,
    Orchestrator,
    RetryPolicy,
    Step,
    TaskState,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    obj_schema,
    parse_arguments,
    render_function_call_results,
    validate_against_schema,
)


@pytest.fixture
def tmp_dir():
    """测试临时目录。

    部分受限环境不允许 pytest 使用系统 temp（会报 EEXIST / 权限错误），
    统一落到项目内 .pytest_tmp，让测试结果只反映代码本身的正确性。
    """
    base = os.environ.get("AGENT_CORE_TEST_TMP")
    if base:
        path = os.path.join(base, "pytest")
    else:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".pytest_tmp")
    os.makedirs(path, exist_ok=True)
    return path


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(
        name="add",
        description="两数求和",
        properties={"a": {"type": "integer"}, "b": {"type": "integer"}},
    )
    def add(a: int, b: int) -> int:
        return a + b

    @reg.tool(
        name="flaky",
        description="前 n 次调用失败",
        properties={"fail_times": {"type": "integer", "default": 1}},
        retryable=True,
    )
    def flaky(fail_times: int = 1) -> str:
        flaky.counter = getattr(flaky, "counter", 0) + 1
        if flaky.counter <= fail_times:
            raise RuntimeError(f"第 {flaky.counter} 次故意失败")
        return f"第 {flaky.counter} 次成功"

    return reg


# ---------- 注册与查询 ----------


def test_registry_register_and_get(registry: ToolRegistry) -> None:
    assert registry.has("add")
    assert registry.get("add").name == "add"
    assert "add" in registry
    assert len(registry) == 2


def test_registry_rejects_duplicate_names() -> None:
    reg = ToolRegistry()
    spec = ToolSpec(name="x", description="d", parameters=obj_schema({}), handler=lambda: None)
    reg.add(spec)
    with pytest.raises(ValueError, match="工具名冲突"):
        reg.add(spec)


def test_unknown_tool_raises(registry: ToolRegistry) -> None:
    with pytest.raises(KeyError, match="未知工具"):
        registry.get("nope")


def test_export_api_formats(registry: ToolRegistry) -> None:
    d = Dispatcher(registry)
    oai = d.openai_tool_list()[0]
    assert oai["type"] == "function"
    assert oai["strict"] is True
    assert oai["parameters"]["additionalProperties"] is False

    anth = d.anthropic_tool_list()[0]
    assert "input_schema" in anth

    mcp = d.mcp_tool_list()[0]
    assert "inputSchema" in mcp


# ---------- strict schema 约束 ----------


def test_obj_schema_follows_strict_rules() -> None:
    s = obj_schema({"a": {"type": "string"}})
    assert s["additionalProperties"] is False
    assert s["required"] == ["a"]  # 所有 properties 都必须进 required


def test_validate_rejects_unknown_field() -> None:
    s = obj_schema({"a": {"type": "string"}})
    with pytest.raises(Exception, match="未声明的字段"):
        validate_against_schema({"a": "x", "b": 1}, s)


def test_validate_rejects_missing_required() -> None:
    s = obj_schema({"a": {"type": "string"}, "b": {"type": "integer"}})
    with pytest.raises(Exception, match="缺少必填字段"):
        validate_against_schema({"a": "x"}, s)


def test_validate_rejects_wrong_type() -> None:
    s = obj_schema({"a": {"type": "integer"}})
    with pytest.raises(Exception, match="类型错误"):
        validate_against_schema({"a": "not-an-int"}, s)


def test_boolean_is_not_integer() -> None:
    """Python 里 bool 是 int 的子类，schema 校验必须显式排除。"""
    s = obj_schema({"a": {"type": "integer"}})
    with pytest.raises(Exception, match="类型错误"):
        validate_against_schema({"a": True}, s)


def test_nullable_field_accepted() -> None:
    s = obj_schema({"a": {"type": ["string", "null"]}})
    validate_against_schema({"a": None}, s)
    validate_against_schema({"a": "x"}, s)


def test_enum_constraint() -> None:
    s = obj_schema({"mode": {"type": "string", "enum": ["fast", "slow"]}})
    validate_against_schema({"mode": "fast"}, s)
    with pytest.raises(Exception, match="取值非法"):
        validate_against_schema({"mode": "turbo"}, s)


def test_nested_object_and_array() -> None:
    s = obj_schema({
        "items": {
            "type": "array",
            "items": obj_schema({"k": {"type": "integer"}}),
        }
    })
    validate_against_schema({"items": [{"k": 1}, {"k": 2}]}, s)
    with pytest.raises(Exception):
        validate_against_schema({"items": [{"k": "bad"}]}, s)


# ---------- arguments 解析 ----------


def test_parse_arguments_accepts_json_string() -> None:
    """API 层 arguments 通常是 JSON 编码的字符串，必须能接住。"""
    assert parse_arguments('{"a": 1}') == {"a": 1}


def test_parse_arguments_accepts_dict_and_empty() -> None:
    assert parse_arguments({"a": 1}) == {"a": 1}
    assert parse_arguments("") == {}
    assert parse_arguments(None) == {}


def test_parse_arguments_rejects_bad_json() -> None:
    from agent_core import ValidationError

    with pytest.raises(ValidationError, match="不是合法 JSON"):
        parse_arguments("{not json")


# ---------- 分发 ----------


def test_dispatch_success(registry: ToolRegistry) -> None:
    result = Dispatcher(registry).dispatch("add", {"a": 1, "b": 2})
    assert result.ok
    assert result.data == 3
    assert json.loads(result.to_model())["data"] == 3


def test_dispatch_unknown_tool_returns_error_not_raise(registry: ToolRegistry) -> None:
    result = Dispatcher(registry).dispatch("ghost", {})
    assert not result.ok
    assert "未知工具" in result.error


def test_dispatch_bad_params_no_retry(registry: ToolRegistry) -> None:
    """参数错了不该重试 —— 重试没有意义，要让 LLM 改参数。"""
    result = Dispatcher(registry).dispatch("add", {"a": "x"})
    assert not result.ok
    assert "参数校验失败" in result.error


def test_dispatch_retries_retryable_failure(registry: ToolRegistry) -> None:
    d = Dispatcher(registry, retry_policy=RetryPolicy(max_attempts=3, initial_backoff=0.01))
    result = d.dispatch("flaky", {"fail_times": 2})
    assert result.ok
    assert "第 3 次成功" in result.data


def test_dispatch_gives_up_after_max_attempts() -> None:
    reg = ToolRegistry()

    @reg.tool(name="always", description="总是失败", properties={}, retryable=True)
    def always() -> None:
        raise RuntimeError("boom")

    d = Dispatcher(reg, retry_policy=RetryPolicy(max_attempts=2, initial_backoff=0.01))
    result = d.dispatch("always", {})
    assert not result.ok
    assert "boom" in result.error


def test_dispatch_handler_raising_typed_error() -> None:
    from agent_core import ExecutionError

    reg = ToolRegistry()

    @reg.tool(name="bad", description="抛类型化错误", properties={})
    def bad() -> None:
        raise ExecutionError("依赖服务不可用", retryable=False)

    result = Dispatcher(reg).dispatch("bad", {})
    assert not result.ok
    assert "依赖服务不可用" in result.error


def test_dispatch_many_preserves_order(registry: ToolRegistry) -> None:
    calls = [("add", {"a": 1, "b": 1}), ("add", {"a": 2, "b": 2}), ("ghost", {})]
    results = Dispatcher(registry).dispatch_many(calls)
    assert len(results) == 3
    assert results[0].data == 2
    assert results[1].data == 4
    assert results[2].ok is False  # 同批中单个失败不影响其它


def test_dispatch_many_empty(registry: ToolRegistry) -> None:
    assert Dispatcher(registry).dispatch_many([]) == []


def test_defaults_filled_from_schema() -> None:
    reg = ToolRegistry()

    @reg.tool(
        name="greet",
        description="打招呼",
        properties={"name": {"type": "string"}, "times": {"type": "integer", "default": 1}},
    )
    def greet(name: str, times: int = 1) -> str:
        return " ".join([f"hi {name}"] * times)

    assert Dispatcher(reg).dispatch("greet", {"name": "a"}).data == "hi a"


# ---------- 结果回传格式 ----------


def test_render_function_call_results_uses_call_id(registry: ToolRegistry) -> None:
    """Responses API 用 call_id 回传，不能错用 function_call.id。"""
    d = Dispatcher(registry)
    calls = [("add", "call_abc123")]
    out = render_function_call_results(calls, d.dispatch_many(calls))
    assert out[0]["type"] == "function_call_output"
    assert out[0]["call_id"] == "call_abc123"
    json.loads(out[0]["output"])  # 必须是合法 JSON 字符串


def test_render_chat_completion_results_uses_tool_call_id(registry: ToolRegistry) -> None:
    from agent_core import render_chat_completion_results

    d = Dispatcher(registry)
    calls = [("add", "call_xyz")]
    out = render_chat_completion_results(calls, d.dispatch_many(calls))
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "call_xyz"


# ---------- 上下文管理 ----------


def test_context_manager_spills_large_result(tmp_dir) -> None:
    cm = ContextManager(max_inline_bytes=200, artifact_dir=tmp_dir)
    big = {"rows": [{"i": i, "payload": "x" * 50} for i in range(100)]}
    result = cm.process(ToolResult.success(big), tool_name="data")
    assert result.artifact_path is not None
    assert "落盘" in result.to_model()


def test_context_manager_keeps_small_result_inline(tmp_dir) -> None:
    cm = ContextManager(max_inline_bytes=10_000, artifact_dir=tmp_dir)
    result = cm.process(ToolResult.success({"a": 1}), tool_name="t")
    assert result.artifact_path is None


def test_context_manager_summarize() -> None:
    s = ContextManager.summarize(list(range(100)), max_items=5)
    assert s["_truncated"] is True
    assert s["_total"] == 100
    assert len(s["_preview"]) == 5


# ---------- 重试策略 ----------


def test_retry_policy_backoff_grows_and_caps() -> None:
    p = RetryPolicy(initial_backoff=1.0, backoff_multiplier=2.0, max_backoff=4.0)
    assert p.delay_for(1) == 1.0
    assert p.delay_for(2) == 2.0
    assert p.delay_for(3) == 4.0
    assert p.delay_for(9) == 4.0  # 上限封顶


def test_retry_policy_calls_on_exhausted() -> None:
    seen = []
    p = RetryPolicy(max_attempts=2, initial_backoff=0.01,
                    on_exhausted=lambda n, e, a: seen.append((n, a)))

    def boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        p.run(boom, retryable=True)
    assert seen and seen[0][1] == 2


# ---------- 编排 ----------


def test_orchestrator_sequential_with_dependency(registry: ToolRegistry) -> None:
    o = Orchestrator(Dispatcher(registry))
    plan = [
        Step(name="s1", instruction="", inputs={"tool": "add", "arguments": {"a": 1, "b": 1}}),
        Step(name="s2", instruction="", inputs={"tool": "add", "arguments": {"a": 2, "b": 2}}),
    ]
    results = o.run(plan)
    assert [r.output for r in results] == [2, 4]


def test_orchestrator_resolves_from_reference(registry: ToolRegistry) -> None:
    o = Orchestrator(Dispatcher(registry))
    plan = [
        Step(name="first", instruction="", inputs={"tool": "add", "arguments": {"a": 3, "b": 4}}),
        Step(name="second", instruction="", inputs={"result": {"$from": "first"}}),
    ]
    results = o.run(plan)
    assert results[1].output == 7


def test_orchestrator_stops_on_error_by_default(registry: ToolRegistry) -> None:
    o = Orchestrator(Dispatcher(registry))
    plan = [
        Step(name="bad", instruction="", inputs={"tool": "ghost", "arguments": {}}),
        Step(name="never", instruction="", inputs={"tool": "add", "arguments": {"a": 1, "b": 1}}),
    ]
    results = o.run(plan)
    assert len(results) == 1
    assert results[0].ok is False


def test_orchestrator_continue_on_error(registry: ToolRegistry) -> None:
    o = Orchestrator(Dispatcher(registry), continue_on_error=True)
    plan = [
        Step(name="bad", instruction="", inputs={"tool": "ghost", "arguments": {}}),
        Step(name="good", instruction="", inputs={"tool": "add", "arguments": {"a": 1, "b": 1}}),
    ]
    results = o.run(plan)
    assert len(results) == 2
    assert results[1].output == 2


def test_orchestrator_enforces_step_tool_allowlist(registry: ToolRegistry) -> None:
    o = Orchestrator(Dispatcher(registry))
    plan = [Step(name="s", instruction="", tools=["nonexistent"],
                 inputs={"tool": "add", "arguments": {"a": 1, "b": 1}})]
    assert o.run(plan)[0].ok is False


def test_orchestrator_parallel(registry: ToolRegistry) -> None:
    o = Orchestrator(Dispatcher(registry))
    plan = [
        Step(name="a", instruction="", inputs={"tool": "add", "arguments": {"a": 1, "b": 1}}),
        Step(name="b", instruction="", inputs={"tool": "add", "arguments": {"a": 2, "b": 2}}),
    ]
    results = o.run_parallel(plan)
    assert sorted(r.output for r in results) == [2, 4]


# ---------- 记忆与任务状态 ----------


def test_memory_store_roundtrip(tmp_dir) -> None:
    path = os.path.join(tmp_dir, "m.json")
    m = MemoryStore(path=path)
    m.put("pref", "Python", scope="global", tags=["lang"])
    assert m.get("pref") == "Python"
    assert m.has("pref")
    m.save()

    m2 = MemoryStore(path=path)
    assert m2.get("pref") == "Python"
    assert len(m2.search(tags=["lang"])) == 1


def test_memory_search_filters_by_scope(tmp_dir) -> None:
    m = MemoryStore(path=os.path.join(tmp_dir, "m.json"))
    m.put("s1", 1, scope="session", tags=["a"])
    m.put("g1", 2, scope="global", tags=["a"])
    assert len(m.search(tags=["a"], scope="global")) == 1


def test_task_state_tracks_progress() -> None:
    st = TaskState("写一个爬虫")
    assert st.progress == 0.0
    st.mark_done("s1", output={"n": 1})
    st.mark_failed("s2", "timeout")
    assert st.is_done("s1")
    assert st.remaining(["s1", "s2", "s3"]) == ["s2", "s3"]
    assert st.progress == 0.5
    assert "爬虫" in st.summary()
