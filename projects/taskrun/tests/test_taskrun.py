"""taskrun 测试。

重点验证：
1. 结果判定的正确性（成功/失败/超时/无法启动四种状态）
2. 超时的诚实性——不把超时伪装成成功，并如实报告进程是否真的终止
3. Cron 与间隔解析的边界
4. 心跳的「死信开关」语义
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

from taskrun import (
    CronSchedule,
    PingType,
    Reporter,
    RunStatus,
    Scheduler,
    format_duration,
    load_pings,
    parse_duration,
    run,
    run_many,
)


@pytest.fixture
def tmp_dir():
    """测试临时目录。

    部分受限环境不允许 pytest 使用系统 temp（报 EEXIST / 权限错误），
    统一落到项目内 .pytest_tmp，让测试结果只反映代码正确性。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".pytest_tmp")
    os.makedirs(path, exist_ok=True)
    return path


@pytest.fixture
def ping_file(tmp_dir):
    """每个用例一个全新的心跳文件。

    心跳是 append 语义，共用文件会让断言被上一次运行的记录污染。
    """
    import uuid

    return os.path.join(tmp_dir, f"hb_{uuid.uuid4().hex[:8]}.jsonl")


# ---------- 执行与判定 ----------

def test_run_success() -> None:
    r = run("echo hi")
    assert r.ok
    assert r.status is RunStatus.SUCCESS
    assert r.exit_code == 0
    assert r.stdout.strip() == "hi"
    assert r.duration_ms >= 0


def test_run_failure_captures_exit_code() -> None:
    r = run("exit 3")
    assert not r.ok
    assert r.status is RunStatus.FAIL
    assert r.exit_code == 3
    assert "退出码 3" in r.verdict


def test_run_captures_stderr() -> None:
    r = run("echo oops 1>&2")
    assert r.stderr.strip() == "oops"


def test_run_separates_streams() -> None:
    r = run("echo out; echo err 1>&2")
    assert r.stdout.strip() == "out"
    assert r.stderr.strip() == "err"


def test_run_list_argv_avoids_shell() -> None:
    """用列表传参时不经过 shell，元字符不会被解释。"""
    r = run(["echo", "$HOME"])
    assert r.stdout.strip() == "$HOME"


def test_run_spawn_error() -> None:
    """列表形式下程序不存在时应报 spawn_error 而不是抛异常。

    字符串形式会经由 shell，命令找不到时 shell 自己返回 127，判为 FAIL；
    只有「连 shell/程序都起不来」才是 SPAWN_ERROR。
    """
    r = run(["definitely_not_a_command_xyz_123"])
    assert r.status is RunStatus.SPAWN_ERROR
    assert not r.ok
    assert "无法启动" in r.verdict


def test_run_shell_missing_command_is_fail() -> None:
    """字符串命令找不到时是 127 退出码，属 FAIL 而非 SPAWN_ERROR。"""
    r = run("definitely_not_a_command_xyz_123")
    assert r.status is RunStatus.FAIL
    assert r.exit_code == 127


def test_run_with_cwd(tmp_dir) -> None:
    r = run("pwd", cwd=tmp_dir)
    assert os.path.realpath(r.stdout.strip()) == os.path.realpath(tmp_dir)


def test_run_with_env() -> None:
    r = run("echo $TASKRUN_TEST_VAR", env={"TASKRUN_TEST_VAR": "hello"})
    assert r.stdout.strip() == "hello"


def test_run_timeout_kills_process() -> None:
    """超时必须强杀，且如实报告已终止。"""
    r = run("sleep 5", timeout=0.4)
    assert r.status is RunStatus.TIMEOUT
    assert r.timed_out is True
    assert r.ok is False
    assert "超时" in r.verdict
    assert r.duration_ms < 4000, "应在超时后立即返回而不是等满 5 秒"


def test_run_timeout_reports_kill_state() -> None:
    """killed 字段必须如实反映进程是否真的终止。"""
    r = run("sleep 5", timeout=0.3)
    assert r.killed is True
    assert r.meta.get("killed") is not True or r.killed is True


def test_run_truncates_huge_output() -> None:
    r = run("python3 -c \"print('x'*100000)\"", max_output_bytes=1000)
    assert len(r.stdout) <= 1000
    assert r.meta.get("output_truncated") is True


def test_run_to_dict_shape() -> None:
    d = run("echo hi").to_dict()
    for key in ("status", "ok", "command", "exit_code", "duration_ms", "verdict"):
        assert key in d


def test_run_to_dict_truncates_output() -> None:
    r = run("python3 -c \"print('y'*50000)\"")
    d = r.to_dict(max_output=100)
    assert "已截断" in d["stdout"]


def test_stdin_is_devnull() -> None:
    """不给 stdin，任务不该卡在等待输入上。"""
    started = time.monotonic()
    r = run("cat", timeout=2.0)
    assert time.monotonic() - started < 3.0
    assert r.status in (RunStatus.SUCCESS, RunStatus.FAIL, RunStatus.TIMEOUT)


# ---------- 并发 ----------

def test_run_many_preserves_order() -> None:
    results = run_many(["echo a", "echo b", "echo c"])
    assert [r.stdout.strip() for r in results] == ["a", "b", "c"]


def test_run_many_reports_all() -> None:
    results = run_many(["echo ok", "exit 1"])
    assert len(results) == 2
    assert sum(1 for r in results if r.ok) == 1


def test_run_many_stop_on_failure() -> None:
    results = run_many(["exit 1", "echo a", "echo b"], stop_on_failure=True)
    assert len(results) >= 1
    assert not results[0].ok


def test_run_many_empty() -> None:
    assert run_many([]) == []


def test_run_many_is_concurrent() -> None:
    """4 个各睡 0.4s 的命令，并行应远快于串行的 1.6s。"""
    started = time.monotonic()
    run_many([f"sleep 0.4" for _ in range(4)], max_workers=4)
    elapsed = time.monotonic() - started
    assert elapsed < 1.2, f"并发执行耗时 {elapsed:.2f}s，看起来是串行"


# ---------- 时长解析 ----------

def test_parse_duration_variants() -> None:
    assert parse_duration("2h24m") == 8640.0
    assert parse_duration("90s") == 90.0
    assert parse_duration("500ms") == 0.5
    assert parse_duration("1d") == 86400.0
    assert parse_duration("30") == 30.0


def test_parse_duration_invalid() -> None:
    with pytest.raises(ValueError, match="无法解析"):
        parse_duration("abc")


def test_parse_duration_empty() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        parse_duration("  ")


def test_format_duration() -> None:
    assert format_duration(8640) == "2h24m0s"
    assert format_duration(90) == "1m30s"
    assert format_duration(45) == "45s"
    assert format_duration(0.5) == "500ms"


# ---------- Cron ----------

def test_cron_every_15_minutes() -> None:
    c = CronSchedule.parse("*/15 * * * *")
    assert sorted(c.minutes) == [0, 15, 30, 45]
    assert c.hours == set(range(24))


def test_cron_specific_hours() -> None:
    c = CronSchedule.parse("0 2,14 * * *")
    assert c.hours == {2, 14}
    assert c.minutes == {0}


def test_cron_range() -> None:
    c = CronSchedule.parse("0 9-17 * * *")
    assert c.hours == set(range(9, 18))


def test_cron_step_in_range() -> None:
    c = CronSchedule.parse("0 0-12/6 * * *")
    assert c.hours == {0, 6, 12}


def test_cron_weekday() -> None:
    c = CronSchedule.parse("0 0 * * 1")
    assert c.weekdays == {1}


def test_cron_matches() -> None:
    c = CronSchedule.parse("0 3 * * *")
    assert c.matches(datetime(2026, 1, 2, 3, 0))
    assert not c.matches(datetime(2026, 1, 2, 4, 0))


def test_cron_day_or_weekday() -> None:
    """日与周同时受限时按 cron 惯例取「或」。"""
    c = CronSchedule.parse("0 0 13 * 5")
    # 13 号是周五：两者都命中
    assert c.matches(datetime(2026, 11, 13, 0, 0))
    # 20 号是周五：只命中周
    assert c.matches(datetime(2026, 11, 20, 0, 0))


def test_cron_next_after() -> None:
    c = CronSchedule.parse("0 3 * * *")
    nxt = c.next_after(datetime(2026, 1, 2, 4, 0))
    assert nxt == datetime(2026, 1, 3, 3, 0)


def test_cron_next_after_same_minute_moves_forward() -> None:
    """当前时刻恰好命中时，下次应是明天同一时刻而非此刻。"""
    c = CronSchedule.parse("0 3 * * *")
    nxt = c.next_after(datetime(2026, 1, 2, 3, 0))
    assert nxt == datetime(2026, 1, 3, 3, 0)


def test_cron_wrong_field_count() -> None:
    with pytest.raises(ValueError, match="需要 5 段"):
        CronSchedule.parse("0 3 * *")


def test_cron_out_of_range() -> None:
    with pytest.raises(ValueError, match="超出范围"):
        CronSchedule.parse("99 3 * * *")


def test_cron_reversed_range() -> None:
    with pytest.raises(ValueError, match="区间反向"):
        CronSchedule.parse("0 17-9 * * *")


def test_cron_bad_step() -> None:
    with pytest.raises(ValueError, match="步长必须为正"):
        CronSchedule.parse("*/0 * * * *")


def test_cron_never_fires_raises() -> None:
    """永不触发的表达式应报错而不是死循环。

    用「2 月 30 日」这种不存在的日期，且把星期约束设为空来排除
    day-or-weekday 的兜底——否则 2 月里任何一个周五都会让它触发。
    """
    c = CronSchedule("0 0 30 2 *", minutes={0}, hours={0}, days={30},
                     months={2}, weekdays=set())
    with pytest.raises(ValueError, match="不会触发"):
        c.next_after(datetime(2026, 1, 1))


# ---------- 心跳 ----------

def test_reporter_writes_start_and_success(ping_file) -> None:
    path = ping_file
    rep = Reporter("job", log_path=path)
    run_id = rep.next_run_id()
    rep.report_start(run_id)
    rep.report_result(run("echo ok"), run_id=run_id)

    types = [p["type"] for p in load_pings(path)]
    assert types == ["start", "success"]


def test_reporter_run_ids_unique() -> None:
    rep = Reporter("job", log_path=os.devnull)
    ids = {rep.next_run_id() for _ in range(100)}
    assert len(ids) == 100, "run id 必须唯一，否则 start 与 end 无法关联"


def test_reporter_marks_failure(ping_file) -> None:
    path = ping_file
    rep = Reporter("job", log_path=path)
    rep.report_result(run("exit 1"))
    assert load_pings(path)[0]["type"] == "fail"


def test_reporter_timeout_maps_to_fail(ping_file) -> None:
    path = ping_file
    rep = Reporter("job", log_path=path)
    rep.report_result(run("sleep 2", timeout=0.3))
    assert load_pings(path)[0]["type"] == "fail"


def test_reporter_summary(ping_file) -> None:
    rep = Reporter("job", log_path=ping_file)
    rep.report_start("r1")
    rep.report_result(run("echo ok"), run_id="r1")
    s = rep.summary()
    assert s["total_pings"] == 2
    assert s["counts"]["success"] == 1
    assert s["healthy"] is True


def test_reporter_unhealthy_without_success(ping_file) -> None:
    """只有 start 没有 success 时应判定为不健康——这是死信开关语义。"""
    rep = Reporter("job", log_path=ping_file)
    rep.report_start("r1")
    assert rep.summary()["healthy"] is False


def test_reporter_last(ping_file) -> None:
    rep = Reporter("job", log_path=ping_file)
    rep.report_start("r1")
    rep.report_result(run("echo ok"), run_id="r1")
    assert rep.last().type is PingType.SUCCESS
    assert rep.last(PingType.START).type is PingType.START


def test_reporter_respects_send_output(ping_file) -> None:
    path = ping_file
    rep = Reporter("job", log_path=path, send_output=False)
    rep.report_result(run("echo secret"))
    assert load_pings(path)[0].get("output") is None


def test_load_pings_missing_file() -> None:
    assert load_pings("/nonexistent/path/pings.jsonl") == []


def test_load_pings_skips_corrupt_lines(tmp_dir) -> None:
    path = os.path.join(tmp_dir, "p8.jsonl")
    with open(path, "w") as fh:
        fh.write('{"type":"success","slug":"a","timestamp":1}\n')
        fh.write("not json\n")
        fh.write('{"type":"fail","slug":"a","timestamp":2}\n')
    got = load_pings(path)
    assert len(got) == 2


# ---------- 调度器 ----------

def test_scheduler_requires_exactly_one_mode() -> None:
    with pytest.raises(ValueError, match="必须且只能指定一个"):
        Scheduler("echo hi")
    with pytest.raises(ValueError, match="必须且只能指定一个"):
        Scheduler("echo hi", every=1, cron=CronSchedule.parse("* * * * *"))


def test_scheduler_run_once() -> None:
    s = Scheduler("echo scheduled", every=0.01)
    result = s.run_once()
    assert result.result.ok
    assert result.result.stdout.strip() == "scheduled"
    assert s.history == [result]


def test_scheduler_max_runs() -> None:
    s = Scheduler("echo x", every=0.01, max_runs=3)
    s.run_forever()
    assert len(s.history) == 3


def test_scheduler_on_result_callback() -> None:
    seen: list[str] = []
    s = Scheduler("echo cb", every=0.01, max_runs=2,
                  on_result=lambda r: seen.append(r.result.stdout.strip()))
    s.run_forever()
    assert seen == ["cb", "cb"]


def test_scheduler_next_run_at() -> None:
    s = Scheduler("echo x", every=60)
    nxt = s.next_run_at()
    assert nxt is not None
    assert 50 <= (nxt - datetime.now()).total_seconds() <= 65


def test_scheduler_cron_next_run_at() -> None:
    s = Scheduler("echo x", cron=CronSchedule.parse("0 3 * * *"))
    assert s.next_run_at() is not None


def test_scheduler_reports_heartbeats(ping_file) -> None:
    path = ping_file
    rep = Reporter("sched", log_path=path)
    s = Scheduler("echo ok", every=0.01, max_runs=2, reporter=rep)
    s.run_forever()
    types = [p["type"] for p in load_pings(path)]
    assert types == ["start", "success", "start", "success"]


def test_scheduler_stop_after_max_runs_even_on_failure() -> None:
    """失败也应计入 max_runs，否则失败任务会无限重试。"""
    s = Scheduler("exit 1", every=0.01, max_runs=2)
    s.run_forever()
    assert len(s.history) == 2


# ---------- agent 集成 ----------

def test_agent_registration() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    for name in ("taskrun_run", "taskrun_run_many", "taskrun_check",
                 "taskrun_heartbeat", "taskrun_schedule_info",
                 "taskrun_split_command"):
        assert reg.has(name), name


def test_agent_schemas_are_strict() -> None:
    from agent_core import ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    for spec in reg.specs():
        s = spec.parameters
        assert s["additionalProperties"] is False, spec.name
        assert set(s["required"]) == set(s["properties"]), spec.name


def test_agent_run() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("taskrun_run", {"command": "echo agent"})
    assert r.ok and r.data["ok"] is True
    assert r.data["stdout"].strip() == "agent"


def test_agent_run_empty_command_fails_gracefully() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("taskrun_run", {"command": "   "})
    assert r.ok is False


def test_agent_run_many() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("taskrun_run_many", {"commands": ["echo a", "echo b"]})
    assert r.ok
    assert r.data["all_ok"] is True
    assert r.data["total"] == 2


def test_agent_check_pass_and_fail() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)

    ok = d.dispatch("taskrun_check", {
        "command": "echo hello", "expect_stdout_contains": "hello",
    })
    assert ok.ok and ok.data["passed"] is True

    bad = d.dispatch("taskrun_check", {
        "command": "echo hello", "expect_stdout_contains": "bye",
    })
    assert bad.data["passed"] is False
    assert bad.data["problems"]


def test_agent_schedule_info_cron_and_interval() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)

    cron = d.dispatch("taskrun_schedule_info", {"expression": "0 3 * * *"})
    assert cron.data["kind"] == "cron"

    interval = d.dispatch("taskrun_schedule_info", {"expression": "2h24m"})
    assert interval.data["kind"] == "interval"
    assert interval.data["seconds"] == 8640


def test_agent_split_command() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("taskrun_split_command", {"command": "echo 'hello world'"})
    assert r.data["valid"] is True
    assert r.data["argv"] == ["echo", "hello world"]


def test_agent_split_command_unbalanced_quotes() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("taskrun_split_command", {"command": "echo 'unclosed"})
    assert r.data["valid"] is False


def test_agent_heartbeat(ping_file) -> None:
    from agent_core import Dispatcher, ToolRegistry

    from taskrun.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    path = ping_file
    r = d.dispatch("taskrun_heartbeat", {"slug": "job", "log_path": path})
    assert r.ok
    assert r.data["total_pings"] == 1
    assert load_pings(path)[0]["type"] == "success"


# ---------- CLI ----------

def _cli(args: list[str], *, allow_fail: bool = False) -> tuple[int, str, str]:
    """跑一次 CLI 并返回 (退出码, stdout, stderr)。

    用子进程而非 monkeypatch，才能真实覆盖参数解析与退出码传播。
    传 -B 避免子进程写出 .pyc 影响后续用例。
    allow_fail=True 时接受非零退出码（用于测试失败路径）。
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "taskrun.cli", *args],
        capture_output=True, text=True, check=False,
    )
    assert allow_fail or proc.returncode == 0 or proc.stderr, (
        f"CLI 意外失败 rc={proc.returncode}\n"
        f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_run_subcommand() -> None:
    code, out, _ = _cli(["run", "echo cli"])
    assert code == 0 and out.strip() == "cli"


def test_cli_remainder_style() -> None:
    code, out, _ = _cli(["--", "echo", "hello"])
    assert code == 0 and out.strip() == "hello"


def test_cli_exit_code_propagates() -> None:
    code, _, _ = _cli(["--", "sh", "-c", "exit 5"], allow_fail=True)
    assert code == 5


def test_cli_check_pass() -> None:
    code, out, _ = _cli(["check", "echo abc", "--contains", "abc"])
    assert code == 0 and "通过" in out


def test_cli_check_fail() -> None:
    code, out, _ = _cli(["check", "echo abc", "--contains", "xyz"], allow_fail=True)
    assert code == 1 and "失败" in out


def test_cli_json_output() -> None:
    import json

    code, out, _ = _cli(["--json", "--", "echo", "js"])
    assert code == 0
    assert json.loads(out)["ok"] is True


def test_cli_no_command_prints_help() -> None:
    code, out, _ = _cli([], allow_fail=True)
    assert code == 1
    assert "usage" in out.lower()


def test_cli_every_and_at_conflict() -> None:
    code, _, err = _cli(["--every", "1h", "--at", "0 3 * * *", "--", "echo", "x"])
    assert code == 1
    assert "不能同时使用" in err


def test_cli_bad_every_expression() -> None:
    code, _, err = _cli(["--every", "abc", "--", "echo", "x"])
    assert code == 1 and "无法解析时长" in err


def test_cli_slug_writes_heartbeat(ping_file) -> None:
    path = ping_file
    code, _, _ = _cli(["--slug", "cli-job", "--ping-file", path, "--", "echo", "ok"])
    assert code == 0
    types = [p["type"] for p in load_pings(path)]
    assert types == ["start", "success"]
