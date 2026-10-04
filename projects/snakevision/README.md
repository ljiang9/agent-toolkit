# snakevision

会自己玩的终端贪吃蛇。BFS/A\* 寻路 + 三级存活策略 + 双缓冲 ANSI 渲染，可当屏保。

复刻自 AngelJumbo 的 [sssnake](https://github.com/AngelJumbo/sssnake)，用纯 Python
重写，去掉了原项目 117KB 的 termbox 依赖，改为纯 ANSI 控制序列。零第三方依赖。

## 快速开始

```bash
pip install -e .

# 自动玩一局
snakevision

# 屏保模式（死亡后自动重开）
snakevision -m screensaver --try-hard

# 跑一帧就退出（适合管道 / 截图）
snakevision --once --style blocks --show-grid

# 跑 500 步输出统计，不实时渲染
snakevision --benchmark 500
```

```
  012345678901234567890123
0                          
1                          
2                          
3           *              
4            oo@           
5                          
6                          
7                          

 长度 3 · 得分 0 · 步数 1 · 运行中
```

## 三级存活策略

朴素贪心有个致命缺陷：**直奔食物会把自己逼进死角**。实测对比（20×12 棋盘）：

| 策略 | 结果 |
|---|---|
| 朴素贪心（只看最短路） | 几十步内自撞死亡 |
| 本项目三级策略 | 存活 200+ 步，长度可长到 63，得分 16 |

三级递进：

1. **贴尾法** —— 算出通往食物的最短路径，走第一步。若走完不会困死，执行。
2. **保命法** —— 食物够不着了，但这一步能保证活着，执行。
3. **试算法（`--try-hard`）** —— 对每个候选动作多步模拟，取存活步数最多的。

第 3 级最稳但最慢，适合长时间挂机。

## 寻路

```python
from snakevision import Grid, Point, bfs_path, astar_path, find_path

g = Grid(11, 11)
for y in range(4):
    g.add_wall(5, y)          # 留出缺口，可绕行

bfs_path(g, Point(0, 0), Point(10, 10))       # 广度优先，保证最短
astar_path(g, Point(0, 0), Point(10, 10))     # A*，更快
astar_path(g, Point(0, 0), Point(10, 10), weighted=True)  # 启发值 ×1.2
find_path(g, Point(0, 0), Point(10, 10), algorithm="bfs")  # 按名选择
```

**确定性**：相同输入永远得到相同路径。这让它可以当 agent 的规划校验器 ——
用来验证某个路径规划方案是否可达、可复现。

`blocked` 参数可指定额外障碍（如蛇身占据的格子）：

```python
bfs_path(g, Point(0, 0), Point(10, 0), blocked={Point(1, 0)})
# 会绕开 (1,0)，不会穿过它
```

## 作为库使用

```python
from snakevision import Game, Direction, render_once, run_autopilot

g = Game.create(30, 15, snake_length=3, seed=42)
g = run_autopilot(g, max_steps=1000)
print(g.score, len(g.snake), g.status)

# 渲染成字符串（不做任何终端 IO）
print(render_once(g, hud=True))

# 手动控制
g.step(Direction.NORTH)

# 快照 / 还原：快照是自包含的纯 JSON
snap = g.snapshot()
g2 = Game.restore(snap)
assert g2.snapshot() == snap
```

固定 `seed` 可让整局完全可复现，适合写测试和演示。

## 渲染

三种风格：

```python
from snakevision import Theme, render_once

render_once(g, theme=Theme.ascii_plain())   # 纯 ASCII 无色（适合日志）
render_once(g, theme=Theme())               # 默认，带颜色
render_once(g, theme=Theme.blocks())        # 方块风格
render_once(g, show_grid=True)              # 带坐标轴
```

**双缓冲**：逐帧重画整屏会闪烁。`Renderer.diff()` 与上一帧比较，
帧相同时返回空串，调用方据此跳过 IO：

```python
r = Renderer()
r.enter_live_mode()
r.live(r.frame(game))    # 输出
r.live(r.frame(game))    # 帧未变 -> 返回空串，不产生输出
r.exit_live_mode()       # 务必恢复光标，否则终端一直不可见
```

## 作为 agent 工具

注册 5 个工具：

```python
from agent_core import ToolRegistry, Dispatcher
from snakevision.agent import register

reg = ToolRegistry()
register(reg)
d = Dispatcher(reg)

# 确定性路径规划
d.dispatch("snakevision_plan_path", {
    "width": 10, "height": 10,
    "start": {"x": 0, "y": 0}, "goal": {"x": 3, "y": 0},
})
# {"ok": true, "data": {"found": true, "path": [...], "length": 3}}

# 创建可复现的对局
created = d.dispatch("snakevision_new_game", {"width": 20, "height": 12, "seed": 42})
snap = created.data["snapshot"]

# 逐步推进
d.dispatch("snakevision_step", {"state": snap})
# {"ok": true, "data": {"snapshot": {...}, "chosen_direction": "SOUTH"}}
```

| 工具 | 用途 |
|---|---|
| `snakevision_plan_path` | BFS/A\* 路径规划，确定性可复现 |
| `snakevision_new_game` | 创建对局，`seed` 保证可复现 |
| `snakevision_step` | 推进一步，返回完整快照 |
| `snakevision_safe_moves` | 列出所有安全方向及各自存活率 |
| `snakevision_render` | 渲染成 ASCII 文本 |

`snakevision_safe_moves` 的价值在于给 agent 一个**可解释的风险指标**，
而不是让它盲目走一步看一步：

```python
{
  "safe_count": 3,
  "recommended": "SOUTH",
  "moves": [
    {"direction": "SOUTH", "reaches_food": true,  "survival": 0.85},
    {"direction": "EAST",  "reaches_food": false, "survival": 0.72},
    ...
  ]
}
```

## 实现的三个坑

写这个项目时踩到的三个隐蔽 bug，都由测试兜住了：

**1. `blocked` 静默失效** —— 查询时写成 `(nx, ny) in blocked`（元组），
但 `blocked` 存的是 `Point` 对象。元组永不等于 `Point`，判断恒为 `False`，
`blocked` 就被完全忽略。修法：构造 `Point(nx, ny)` 再比较。

**2. 蛇头算进了障碍** —— `snake.occupied()` 包含 `body[0]`（蛇头自己），
传给寻路后「起点不可通行」直接判无解，路径永远为空，蛇只会绕圈。
修法：`_target_blocked()` 排除 `body[0]`。

**3. 头尾颠倒** —— `Snake.create()` 里多做了一次 `reverse()`，
导致 `body[0]` 是尾而不是头，蛇的移动方向与身体延伸方向相反。
修法：去掉 `reverse()`。

前两个都是「代码看起来对、结果全错」的静默失效，靠读代码发现不了。

## 目录结构

```
snakevision/
├── snakevision/
│   ├── __init__.py     # 公开 API
│   ├── grid.py         # 网格与坐标（越界即墙）
│   ├── pathfind.py     # BFS / A*，可替换算法
│   ├── game.py         # 蛇状态机、碰撞、快照往返
│   ├── autopilot.py    # 三级存活策略
│   ├── render.py       # 双缓冲 ANSI 渲染
│   ├── agent.py        # agent 工具注册（5 个）
│   └── cli.py          # 命令行
├── tests/
│   └── test_snakevision.py  # 61 个测试
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
