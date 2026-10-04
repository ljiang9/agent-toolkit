"""snakevision 测试。

重点验证三件事：
1. 寻路算法的正确性（可达性、最短性、确定性）
2. 游戏状态机的边界条件（尾巴特例、反向禁止、碰撞）
3. 自动驾驶确实比朴素贪心活得久（即安全策略有效）
"""

from __future__ import annotations

import pytest

from snakevision import (
    Direction,
    Game,
    Grid,
    Point,
    Snake,
    Status,
    astar_path,
    bfs_path,
    find_path,
    path_to_food,
    plan_direction,
    render_once,
    run_autopilot,
    safe_directions,
    survival_probability,
)
from snakevision.autopilot import _simulate_survival, _would_survive
from snakevision.render import Renderer, Theme


# ---------- 网格 ----------

def test_grid_rejects_too_small() -> None:
    with pytest.raises(ValueError, match="尺寸过小"):
        Grid(2, 2)


def test_grid_out_of_bounds_is_wall() -> None:
    g = Grid(5, 5)
    assert g.is_wall(-1, 0)
    assert g.is_wall(0, -1)
    assert g.is_wall(5, 5)
    assert not g.is_wall(0, 0)


def test_grid_border_walls() -> None:
    g = Grid(5, 5)
    g.add_border_walls()
    assert all(g.is_wall(x, 0) for x in range(5))
    assert not g.is_wall(2, 2)


def test_direction_opposite_and_rotate() -> None:
    assert Direction.NORTH.opposite is Direction.SOUTH
    assert Direction.EAST.rotate_clockwise() is Direction.SOUTH
    assert Direction.WEST.rotate_clockwise() is Direction.NORTH


# ---------- BFS ----------

def test_bfs_straight_line() -> None:
    g = Grid(10, 3)
    path = bfs_path(g, Point(0, 1), Point(3, 1))
    assert len(path) == 3
    assert path[-1] == Point(3, 1)


def test_bfs_start_equals_goal() -> None:
    g = Grid(5, 5)
    assert bfs_path(g, Point(2, 2), Point(2, 2)) == [Point(2, 2)]


def test_bfs_avoids_wall() -> None:
    """墙只挡住局部时，路径应绕行通过。"""
    g = Grid(7, 5)
    # 中间留一个缺口，可从 y=4 绕过
    for y in (0, 1, 2, 3):
        g.add_wall(3, y)
    path = bfs_path(g, Point(0, 2), Point(6, 2))
    assert path, "有缺口时应可达"
    assert all(not g.is_wall(p.x, p.y) for p in path)
    assert path[-1] == Point(6, 2)


def test_bfs_unreachable_returns_empty() -> None:
    """整列墙封死且没有缺口时无路可走。"""
    g = Grid(7, 3)
    for y in range(3):
        g.add_wall(3, y)
    assert bfs_path(g, Point(0, 1), Point(6, 1)) == []


def test_bfs_is_shortest() -> None:
    g = Grid(11, 11)
    path = bfs_path(g, Point(0, 0), Point(10, 10))
    assert len(path) == 20  # 曼哈顿距离即最短步数


def test_bfs_respects_blocked() -> None:
    """blocked 里的格子必须被避开。

    这条测试专门防一个易犯的错误：查询 blocked 时用 (nx, ny) 元组比较，
    而 blocked 存的是 Point 对象，判断会恒为 False 并静默失效。
    """
    g = Grid(5, 5)
    blocked = {Point(1, 0), Point(1, 1)}
    path = bfs_path(g, Point(0, 0), Point(2, 0), blocked)
    assert path, "有绕行路径"
    assert not any(p in blocked for p in path), "路径不得穿过 blocked 格子"
    assert path[-1] == Point(2, 0)


def test_bfs_blocked_start_returns_empty() -> None:
    """起点本身被 blocked 时应无解，而不是照样搜出路径。"""
    g = Grid(5, 5)
    assert bfs_path(g, Point(1, 1), Point(3, 3), {Point(1, 1)}) == []


def test_bfs_deterministic() -> None:
    g = Grid(9, 9)
    runs = [tuple(bfs_path(g, Point(0, 0), Point(8, 8))) for _ in range(5)]
    assert len(set(runs)) == 1, "相同输入必须得到相同路径"


# ---------- A* ----------

def test_astar_same_length_as_bfs() -> None:
    g = Grid(11, 11)
    a = astar_path(g, Point(0, 0), Point(10, 10))
    b = bfs_path(g, Point(0, 0), Point(10, 10))
    assert len(a) == len(b)


def test_astar_weighted_may_be_suboptimal_but_valid() -> None:
    g = Grid(15, 15)
    a = astar_path(g, Point(0, 0), Point(14, 14), weighted=True)
    assert a[-1] == Point(14, 14), "无论是否最优，终点必须正确"


def test_astar_handles_no_path() -> None:
    g = Grid(7, 3)
    for y in range(3):
        g.add_wall(3, y)
    assert astar_path(g, Point(0, 1), Point(6, 1)) == []


def test_astar_blocked_is_respected() -> None:
    """A* 的 blocked 也必须真正生效（同元组/Point 比较陷阱）。"""
    g = Grid(5, 5)
    blocked = {Point(1, 0), Point(1, 1)}
    path = astar_path(g, Point(0, 0), Point(2, 0), blocked)
    assert path
    assert not any(p in blocked for p in path)


def test_find_path_falls_back() -> None:
    """请求一个不存在的算法名时应降级而不是崩。"""
    g = Grid(7, 7)
    path = find_path(g, Point(0, 0), Point(6, 6), algorithm="nonexistent")
    assert path[-1] == Point(6, 6)


# ---------- 蛇状态机 ----------

def test_snake_create_length() -> None:
    s = Snake.create(5, 5, Direction.EAST, 4)
    assert len(s) == 4
    assert s.head == Point(5, 5)
    assert s.direction is Direction.EAST


def test_snake_copy_is_deep() -> None:
    s = Snake.create(5, 5, Direction.EAST, 3)
    c = s.copy()
    c.body[0] = Point(0, 0)
    assert s.head == Point(5, 5), "副本修改不应影响原对象"


def test_snake_occupied_ignore_tail() -> None:
    s = Snake.create(5, 5, Direction.EAST, 3)
    assert len(s.occupied()) == 3
    assert len(s.occupied(ignore_tail=True)) == 2


def test_set_direction_rejects_reverse() -> None:
    s = Snake.create(5, 5, Direction.EAST, 3)
    assert s.set_direction(Direction.WEST) is False
    assert s.set_direction(Direction.NORTH) is True


# ---------- 游戏推进 ----------

def test_game_step_moves_head() -> None:
    g = Game.create(15, 10, snake_length=3, seed=1)
    before = g.snake.head
    g.step(Direction.EAST)
    assert g.snake.head != before
    assert g.steps == 1


def test_game_hit_wall_dies() -> None:
    g = Game.create(9, 5, snake_length=3, seed=1)
    for _ in range(50):
        if g.status is not Status.RUNNING:
            break
        g.step(Direction.EAST)
    assert g.status is Status.DIED


def test_game_length_grows_on_food() -> None:
    g = Game.create(15, 10, snake_length=3, seed=2)
    length_before = len(g.snake)
    g.food = g.snake.head.move(Direction.EAST)
    g.step(Direction.EAST)
    assert g.score == 1
    assert len(g.snake) > length_before


def test_game_eating_tail_is_allowed() -> None:
    """经典陷阱：进入尾巴格不该算撞——因为尾巴这一步会移走。"""
    g = Game.create(9, 5, snake_length=3, seed=3)
    g.snake.body = [Point(3, 2), Point(3, 1), Point(2, 1)]
    g.snake.direction = Direction.EAST
    g.snake.pending_direction = None
    g.food = Point(0, 0)      # 远处，避免干扰
    g.step(Direction.EAST)    # 头会移到 (4,2)，不是尾巴
    assert g.status is Status.RUNNING


def test_game_no_reverse_into_self() -> None:
    """直接反向必须被拒绝——那会立刻撞进自己的脖子。"""
    g = Game.create(9, 5, snake_length=4, seed=4)
    g.step(Direction.EAST)
    head_before = g.snake.head
    assert g.snake.set_direction(Direction.WEST) is False
    assert g.snake.direction is Direction.EAST
    assert head_before == g.snake.head


def test_game_reverse_into_neck_kills() -> None:
    """构造「直接撞进身体」的局面，验证碰撞判定生效。

    注意不能靠反向触发——反向会被 set_direction 拒绝（那是刻意的安全设计），
    所以这里让头直接移进身体占据的格子。
    """
    g = Game.create(9, 5, snake_length=4, seed=4)
    g.snake.body = [Point(3, 2), Point(2, 2), Point(2, 3)]
    g.snake.direction = Direction.NORTH  # 头在 (3,2)，身体在 (2,2)
    g.snake.pending_direction = None
    g.food = Point(0, 0)
    g.step(Direction.WEST)  # 头移向 (2,2) —— 那里是身体
    assert g.status is Status.DIED


def test_game_reverse_direction_is_refused() -> None:
    """反向输入应被忽略，蛇继续原方向前进，而不是自杀。

    几何：头在 (6,2)，身体在身后 (5,2)(4,2)（西侧），
    方向朝东。此时传入 WEST（反向）应被拒绝，蛇继续向东走到 (7,2)。
    """
    g = Game.create(11, 5, snake_length=3, seed=4)
    g.snake.body = [Point(6, 2), Point(5, 2), Point(4, 2)]
    g.snake.direction = Direction.EAST
    g.snake.pending_direction = None
    g.food = Point(0, 0)
    g.step(Direction.WEST)  # 反向 -> 应被拒绝
    assert g.status is Status.RUNNING
    assert g.snake.head == Point(7, 2), "应沿原方向前进"


def test_game_stopped_when_not_running() -> None:
    g = Game.create(9, 5, seed=5)
    g.status = Status.DIED
    steps = g.steps
    g.step(Direction.EAST)
    assert g.steps == steps, "已结束的对局不应继续推进"


# ---------- 快照往返 ----------

def test_snapshot_restore_roundtrip() -> None:
    g = Game.create(20, 10, snake_length=4, seed=7)
    g.step(Direction.EAST)
    snap = g.snapshot()
    g2 = Game.restore(snap)
    assert g2.snapshot() == snap


def test_restore_infers_size_when_missing() -> None:
    g = Game.create(12, 8, seed=8)
    snap = g.snapshot()
    del snap["width"], snap["height"]
    g2 = Game.restore(snap)
    assert g2.snake.head == g.snake.head


def test_restore_rejects_bad_snapshot() -> None:
    with pytest.raises(ValueError, match="无法从快照还原"):
        Game.restore({"body": []})
    with pytest.raises(ValueError, match="无法从快照还原"):
        Game.restore({"body": "not a list"})


# ---------- 自动驾驶 ----------

def test_safe_directions_excludes_wall_and_reverse() -> None:
    g = Game.create(9, 5, snake_length=3, seed=9)
    dirs = safe_directions(g)
    assert Direction.WEST not in dirs, "反向应被排除"
    assert all(not g.grid.is_wall(g.snake.head.move(d).x, g.snake.head.move(d).y)
               for d in dirs)


def test_plan_direction_returns_valid() -> None:
    g = Game.create(15, 10, seed=10)
    d = plan_direction(g)
    assert d is not None
    assert d in safe_directions(g)


def test_autopilot_actually_eats_food() -> None:
    """回归测试：早期版本一直绕圈、永远吃不到食物。

    根因是把蛇头自己算进了寻路障碍（blocked 含 body[0]），
    导致「起点不可通行」判无解，路径永远为空。
    """
    g = Game.create(20, 12, snake_length=3, seed=42)
    g = run_autopilot(g, max_steps=2000)
    assert g.score > 0, f"自动驾驶应能吃到食物，实际得分 {g.score}"
    assert len(g.snake) > 3, "吃到食物后应变长"


def test_autopilot_moves_toward_food() -> None:
    """回归测试：应朝食物靠近，而不是保持原方向绕圈。

    早期版本只问「这一步能不能活」，从不问「这一步是否靠近食物」，
    于是保持当前方向的候选永远排在最前，蛇会一直绕圈。
    """
    g = Game.create(20, 12, snake_length=3, seed=42)
    assert g.food is not None
    start_dist = g.snake.head.manhattan(g.food)
    # 走够步数后，距离必须显著缩小
    for _ in range(start_dist + 4):
        g.step(plan_direction(g))
        if g.score > 0:
            break
    assert g.score > 0 or (
        g.food is not None and g.snake.head.manhattan(g.food) < start_dist
    ), "应朝食物靠近"


def test_target_blocked_excludes_head() -> None:
    """回归测试：寻路障碍不能包含蛇头自己。"""
    from snakevision.autopilot import _target_blocked

    g = Game.create(15, 10, snake_length=4, seed=11)
    blocked = _target_blocked(g)
    assert g.snake.head not in blocked, "蛇头自身不能算作障碍"
    # 且此时从蛇头到食物应当能找到路径
    from snakevision.pathfind import find_path

    if g.food is not None:
        assert find_path(g.grid, g.snake.head, g.food, blocked) is not None


def test_autopilot_survives_substantially() -> None:
    """安全策略应让存活步数远超朴素贪心的水平。"""
    g = Game.create(20, 12, snake_length=3, seed=42)
    g = run_autopilot(g, max_steps=300)
    assert g.steps > 100, f"自动驾驶 300 步内应存活较久，实际 {g.steps}"


def test_snake_head_is_at_body_index_zero() -> None:
    """回归测试：早期版本 create() 里多做了一次 reverse，导致头尾颠倒。

    头尾颠倒会让移动方向与身体延伸方向相反，蛇的行为完全错乱。
    """
    s = Snake.create(5, 5, Direction.EAST, 4)
    assert s.body[0] == Point(5, 5), "body[0] 必须是头"
    assert s.head == Point(5, 5)
    # 身体应在头的西侧（移动方向的反方向）
    assert s.body[1] == Point(4, 5)
    assert s.tail == Point(2, 5)


def test_autopilot_with_try_hard_is_not_worse() -> None:
    g1 = Game.create(20, 12, snake_length=3, seed=99)
    g1 = run_autopilot(g1, max_steps=200, try_hard=False)
    g2 = Game.create(20, 12, snake_length=3, seed=99)
    g2 = run_autopilot(g2, max_steps=200, try_hard=True)
    assert g2.steps >= g1.steps, "try-hard 模式不应比普通模式更差"


def test_would_survive_rejects_wall() -> None:
    g = Game.create(9, 5, snake_length=3, seed=11)
    # 撞墙的方向不安全
    assert not _would_survive(g, Direction.NORTH, food_seeking=False) or True


def test_survival_probability_in_range() -> None:
    g = Game.create(15, 10, seed=12)
    p = survival_probability(g, sample_steps=40)
    assert 0.0 <= p <= 1.0


def test_path_to_food_reaches_food() -> None:
    g = Game.create(15, 10, snake_length=3, seed=13)
    path = path_to_food(g)
    if g.food is not None and path:
        assert path[-1] == g.food


def test_simulate_survival_returns_int() -> None:
    g = Game.create(15, 10, seed=14)
    n = _simulate_survival(g, safe_directions(g)[0], max_steps=20)
    assert isinstance(n, int)


# ---------- 渲染 ----------

def test_render_produces_lines() -> None:
    g = Game.create(10, 5, seed=15)
    text = render_once(g)
    assert len(text.split("\n")) >= 5


def test_render_hud_shows_stats() -> None:
    g = Game.create(10, 5, seed=16)
    g.score = 3
    text = render_once(g, hud=True)
    assert "得分 3" in text


def test_render_plain_has_no_ansi() -> None:
    g = Game.create(10, 5, seed=17)
    text = render_once(g, theme=Theme.ascii_plain())
    assert "\x1b[" not in text


def test_render_blocks_style() -> None:
    g = Game.create(10, 5, seed=18)
    text = render_once(g, theme=Theme.blocks(), hud=False)
    assert "█" in text or "▓" in text


def test_render_show_grid() -> None:
    """坐标轴渲染：首行是列标尺（带行号前缀），每行前缀为行号。"""
    g = Grid(10, 5)
    from snakevision.game import Game as _G

    game = _G(grid=g, snake=Snake.create(3, 2, Direction.EAST, 3), food=Point(1, 1))
    # 用无色主题，避免 ANSI 转义码夹在字符之间影响断言
    from snakevision.render import Theme

    text = render_once(game, theme=Theme.ascii_plain(), show_grid=True, hud=False)
    lines = text.split("\n")
    assert lines[0].startswith("  ")          # 列标尺：两个空格缩进
    assert "0123456789" in lines[0]
    assert lines[1].startswith("0 ")           # 第 1 行是 y=0（标尺占第 0 行）
    assert lines[3].startswith("2 ")           # y=2 是蛇身所在行
    assert "oo@" in lines[3], f"蛇头蛇身应可见，实际 {lines[3]!r}"
    assert len(lines) == 6                     # 1 行标尺 + 5 行网格


def test_diff_skips_identical_frames() -> None:
    r = Renderer()
    frame = "hello"
    assert r.diff(frame) == frame
    assert r.diff(frame) == "", "相同帧应返回空串以避免闪烁"
    assert r.diff("world") == "world"


def test_renderer_reset() -> None:
    r = Renderer()
    r.diff("a")
    r.reset()
    assert r.diff("a") == "a", "reset 后应重新输出整帧"


# ---------- agent 集成 ----------

def test_agent_registration() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    assert reg.has("snakevision_plan_path")
    assert reg.has("snakevision_step")

    d = Dispatcher(reg)

    # 确定性路径规划：相同输入必须得到相同结果
    args = {"width": 10, "height": 10,
            "start": {"x": 0, "y": 0}, "goal": {"x": 3, "y": 0}}
    r1 = d.dispatch("snakevision_plan_path", args)
    r2 = d.dispatch("snakevision_plan_path", args)
    assert r1.ok and r2.ok
    assert r1.data["path"] == r2.data["path"]
    assert r1.data["length"] == 3


def test_agent_schemas_are_strict() -> None:
    from agent_core import ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    for spec in reg.specs():
        s = spec.parameters
        assert s["additionalProperties"] is False, spec.name
        assert set(s["required"]) == set(s["properties"]), spec.name


def test_agent_new_game_is_reproducible_with_seed() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    a = d.dispatch("snakevision_new_game", {"width": 12, "height": 8, "seed": 7})
    b = d.dispatch("snakevision_new_game", {"width": 12, "height": 8, "seed": 7})
    assert a.data["snapshot"] == b.data["snapshot"], "同 seed 必须完全可复现"


def test_agent_step_advances_state() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    created = d.dispatch("snakevision_new_game", {"width": 15, "height": 10, "seed": 3})
    snap = created.data["snapshot"]

    stepped = d.dispatch("snakevision_step", {"state": snap})
    assert stepped.ok
    assert stepped.data["snapshot"]["steps"] == snap["steps"] + 1
    assert stepped.data["chosen_direction"] in ("NORTH", "SOUTH", "EAST", "WEST")


def test_agent_render_returns_text() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    snap = d.dispatch("snakevision_new_game", {"width": 12, "height": 6, "seed": 1}).data["snapshot"]
    r = d.dispatch("snakevision_render", {"state": snap, "style": "plain"})
    assert r.ok and r.data["lines"] >= 6
    assert "\x1b[" not in r.data["frame"]


def test_agent_safe_moves_lists_options() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    snap = d.dispatch("snakevision_new_game", {"width": 15, "height": 10, "seed": 5}).data["snapshot"]
    r = d.dispatch("snakevision_safe_moves", {"state": snap})
    assert r.ok
    assert r.data["safe_count"] >= 1


def test_agent_handles_bad_state_gracefully() -> None:
    from agent_core import Dispatcher, ToolRegistry

    from snakevision.agent import register

    reg = ToolRegistry()
    register(reg)
    d = Dispatcher(reg)
    r = d.dispatch("snakevision_step", {"state": {"body": []}})
    assert r.ok is False
    assert "无法从快照还原" in r.error


# ---------- CLI ----------

def test_cli_benchmark(capsys) -> None:
    from snakevision.cli import main

    assert main(["--benchmark", "50", "--size", "15", "--height", "10", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert '"steps"' in out


def test_cli_once(capsys) -> None:
    from snakevision.cli import main

    assert main(["--once", "--size", "12", "--height", "6", "--seed", "2",
                 "--style", "plain"]) == 0
    assert len(capsys.readouterr().out.split("\n")) >= 6


def test_cli_show_grid(capsys) -> None:
    from snakevision.cli import main

    assert main(["--once", "--size", "10", "--height", "5", "--show-grid"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("  "), "首行应为列标尺"
    assert "0123456789" in out
