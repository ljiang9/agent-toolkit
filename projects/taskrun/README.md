# taskrun

子进程执行、结果判定与心跳上报。带零依赖的 Cron 调度。

复刻自 [bdd/runitor](https://github.com/bdd/runitor)（0BSD），补上更完整的
结构化结果、离线可用的心跳后端与 agent 工具接入。零第三方依赖。

**核心契约**：执行 → 判定 → 上报 三步形成可重试闭环。
超时**不会**被伪装成成功，而是如实报告「超时了，且进程是否确实终止」。

## 快速开始

```bash
pip install -e .

# 执行一次
taskrun -- echo hi
# hi

# JSON 输出，带结构化判定
taskrun --json -- sh -c 'echo out; echo err 1>&2; exit 2'
```

```json
{
  "status": "fail",
  "ok": false,
  "command": "sh -c 'echo out; echo err 1>&2; exit 2'",
  "exit_code": 2,
  "duration_ms": 15,
  "verdict": "命令失败，退出码 2",
  "stdout": "out\n",
  "stderr": "err\n"
}
```

## 四种结果状态

| 状态 | 含义 | 退出码 |
|---|---|---|
| `success` | 退出码 0 | 0 |
| `fail` | 退出码非 0 | 原样透传 |
| `timeout` | 超时被强制终止 | 124 |
| `spawn_error` | 连 shell/程序都起不来 | 127 |

```python
from taskrun import run

r = run("pytest -q")
r.ok              # False
r.status          # RunStatus.FAIL
r.exit_code       # 1
r.verdict         # '命令失败，退出码 1'
```

退出码透传让 shell 脚本能判断具体失败原因；超时统一用 124（与 GNU `timeout` 一致）。

## 超时的诚实性

这是本项目最重要的设计取舍：

```python
r = run("sleep 5", timeout=0.4)
r.status       # RunStatus.TIMEOUT
r.timed_out    # True
r.killed       # True  ← 进程是否**确实**被终止
r.verdict      # '命令超时（415ms）'
```

若 kill 后进程仍在运行（极少见的不可中断 IO），`verdict` 会明确追加
「未能确认进程已终止，结果状态未知」——**不把不确定性藏起来**。

杀的是整个进程组（`start_new_session=True` + `killpg`），孙进程不会变孤儿继续跑。

## 作为库使用

```python
from taskrun import run, run_many

# 单次
r = run("git status --short", cwd="/path/to/repo", timeout=10)

# 列表形式不经 shell，元字符不会被解释（更安全）
r = run(["git", "status", "--short"])

# 并发执行多条，结果顺序与输入一致
results = run_many(["pytest -q", "ruff check .", "mypy ."], max_workers=3)
```

## 断言校验

比手动读输出更可靠：

```python
from agent_core import Dispatcher, ToolRegistry
from taskrun.agent import register

reg = ToolRegistry(); register(reg)
d = Dispatcher(reg)

d.dispatch("taskrun_check", {
    "command": "grep -c error app.log",
    "expect_exit_code": 0,
})
```

## 心跳与「死信开关」

心跳记录定时任务的状态。**定时任务没按时跑，往往比跑了但失败更严重** ——
这正是 healthchecks.io 这类服务的核心价值。

```python
from taskrun import Reporter, run

rep = Reporter("db-backup")             # 默认写 ~/.taskrun/pings.jsonl
run_id = rep.next_run_id()
rep.report_start(run_id)                # 开始
rep.report_result(run("./backup.sh"), run_id=run_id)   # 结束

print(rep.summary())
# {'slug': 'db-backup', 'total_pings': 2, 'counts': {...},
#  'last_success_age_seconds': 0.3, 'healthy': True}
```

`report_result` 会按执行结果自动选 `success` / `fail`。

`run_id` 把 start 与 end 关联起来 —— 时间戳 + 序号保证同秒内多次运行也不撞。

默认后端是本地文件，**不发任何网络请求**，离线可用。传 `url` 则切到 HTTP 上报：

```python
rep = Reporter("db-backup", url="https://hc-ping.com")
```

## 调度

两种模式，对齐原项目的 `--every` 与 `--at`：

```bash
taskrun --slug db-backup --every 2h24m -- /script/backup
taskrun --slug backup --at "0 2,14 * * *" -- /script/backup
```

```python
from taskrun import CronSchedule, Scheduler

cron = CronSchedule.parse("0 2,14 * * *")
s = Scheduler("./backup.sh", cron=cron, max_runs=5)
s.next_run_at()      # 下次触发时间
s.run_forever()      # 到 max_runs 或收到停止信号
```

Cron 解析支持 `*`、`,`、`-`、`/`，零依赖：

```python
CronSchedule.parse("*/15 * * * *").minutes    # {0, 15, 30, 45}
CronSchedule.parse("0 9-17 * * *").hours      # 9..17
```

永不触发的表达式（如 2 月 30 日）会明确报错，而不是死循环。

**SIGALRM 立刻触发一次** —— 不必重启进程就能强制跑：

```python
import os, signal
signal.send_signal(pid, signal.SIGALRM)
```

## 作为 agent 工具

注册 6 个工具：

| 工具 | 用途 |
|---|---|
| `taskrun_run` | 执行命令，返回结构化结果 |
| `taskrun_run_many` | 并发执行多条命令 |
| `taskrun_check` | 执行 + 校验退出码与输出子串 |
| `taskrun_heartbeat` | 上报心跳，返回健康度 |
| `taskrun_schedule_info` | 解析调度表达式，算下次触发时间 |
| `taskrun_split_command` | 按 shell 规则拆分命令为 argv |

`taskrun_run` 标记为 `retryable=True` —— 网络抖动类的瞬时失败值得重试，
而参数错误不会（由 schema 校验拦下）。

```python
d.dispatch("taskrun_schedule_info", {"expression": "2h24m"})
# {"ok": true, "data": {"kind": "interval", "seconds": 8640.0, ...}}
```

## 目录结构

```
taskrun/
├── taskrun/
│   ├── __init__.py     # 公开 API
│   ├── runner.py       # 子进程执行、超时强杀、输出截断
│   ├── reporter.py     # 心跳上报（本地文件 / HTTP）
│   ├── scheduler.py    # 间隔与 Cron 调度、信号处理
│   ├── agent.py        # agent 工具注册（6 个）
│   └── cli.py          # 命令行
├── tests/
│   └── test_taskrun.py  # 76 个测试
├── pyproject.toml
└── README.md
```

## 测试

```bash
pip install -e ".[dev]"
pytest -v
```

## 依赖

| 类型 | 依赖 |
|---|---|
| 运行时 | **无**（仅标准库） |
| 开发 | pytest, pytest-cov, mypy, ruff |
| 可选 | agent-core（启用 agent 工具注册） |

要求 Python ≥ 3.11。

## 许可

MIT
