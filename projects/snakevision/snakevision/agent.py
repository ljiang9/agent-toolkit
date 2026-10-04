"""snakevision 的 agent 工具接入层。

把网格规划能力注册为 agent 工具。核心价值在于：**给 agent 一个确定性的
规划验证器**——同一个 grid 与起终点永远得到同一条路径，可用来校验 agent
自己规划的确定性任务。
"""

from __future__ import annotations

from typing import Any

from agent_core import ToolRegistry

from .autopilot import path_to_food, plan_direction, safe_directions, survival_probability
from .game import Game, Status
from .grid import Direction, Grid, Point
from .render import Theme, render_once


def _grid_from_args(width: int, height: int, walls: list[dict[str, int]] | None) -> Grid:
    grid = Grid(width, height)
    for w in walls or []:
        grid.add_wall(w["x"], w["y"])
    return grid


def register(registry: ToolRegistry) -> None:
    """把 snakevision 的能力注册进工具注册表。"""

    @registry.tool(
        name="snakevision_plan_path",
        description=(
            "在矩形网格上用 BFS 或 A* 计算从起点到终点的最短路径（上下左右四邻域）。"
            "障碍物可以是墙或被占用的格子。当需要确定性地规划一条避开障碍的路径、"
            "或验证某个路径规划方案是否可达时使用。相同输入永远得到相同路径，"
            "适合作为规划结果的校验器。"
        ),
        properties={
            "width": {"type": "integer", "description": "网格宽度，最小 3"},
            "height": {"type": "integer", "description": "网格高度，最小 3"},
            "start": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
                "additionalProperties": False,
                "description": "起点坐标",
            },
            "goal": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
                "additionalProperties": False,
                "description": "终点坐标",
            },
            "walls": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
                "default": None,
                "description": "额外障碍物坐标列表",
            },
            "algorithm": {
                "type": "string",
                "enum": ["bfs", "astar", "astar-weighted"],
                "default": "astar",
                "description": "寻路算法，默认 A*",
            },
        },
    )
    def snakevision_plan_path(
        width: int,
        height: int,
        start: dict[str, int],
        goal: dict[str, int],
        walls: list[dict[str, int]] | None = None,
        algorithm: str = "astar",
    ) -> dict[str, Any]:
        from .pathfind import find_path

        grid = _grid_from_args(width, height, walls)
        s, g = Point(start["x"], start["y"]), Point(goal["x"], goal["y"])
        path = find_path(grid, s, g, algorithm=algorithm)
        return {
            "found": bool(path) or s == g,
            "path": [{"x": p.x, "y": p.y} for p in path],
            "length": len(path),
            "algorithm": algorithm,
        }

    @registry.tool(
        name="snakevision_new_game",
        description=(
            "创建一个新的贪吃蛇对局（指定网格尺寸、蛇的初始长度与随机种子）。"
            "当需要演示搜索算法、验证路径规划，或作为规划任务的沙盒环境时使用。"
            "固定 seed 可让实验完全可复现。"
        ),
        properties={
            "width": {"type": "integer", "default": 30, "description": "网格宽度"},
            "height": {"type": "integer", "default": 15, "description": "网格高度"},
            "snake_length": {"type": "integer", "default": 3, "description": "蛇的初始长度"},
            "seed": {
                "type": ["integer", "null"],
                "default": None,
                "description": "随机种子，传整数可让整局完全可复现",
            },
        },
    )
    def snakevision_new_game(
        width: int = 30,
        height: int = 15,
        snake_length: int = 3,
        seed: int | None = None,
    ) -> dict[str, Any]:
        game = Game.create(width, height, snake_length=snake_length, seed=seed)
        return {"snapshot": game.snapshot()}

    @registry.tool(
        name="snakevision_step",
        description=(
            "推进贪吃蛇对局一步：可指定方向，方向为空时由自动驾驶决策。"
            "返回推进后的完整状态快照（头/身体/食物/得分/存活状态）。"
            "当需要逐步观察搜索算法如何在动态环境中避障时使用。"
        ),
        properties={
            "state": {
                "type": "object",
                "description": "当前状态快照，来自 snakevision_new_game 或上一步的返回值",
            },
            "direction": {
                "type": ["string", "null"],
                "enum": ["NORTH", "SOUTH", "EAST", "WEST", None],
                "default": None,
                "description": "移动方向，留空则由自动驾驶决定",
            },
            "try_hard": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "是否启用多步模拟来挑最安全的动作（更慢但更稳）",
            },
        },
    )
    def snakevision_step(
        state: dict[str, Any],
        direction: str | None = None,
        try_hard: bool | None = None,
    ) -> dict[str, Any]:
        game = _restore(state)
        chosen = None if direction is None else Direction[direction]
        if chosen is None:
            chosen = plan_direction(game, try_hard=bool(try_hard))
        game.step(chosen)
        return {"snapshot": game.snapshot(), "chosen_direction": chosen.name if chosen else None}

    @registry.tool(
        name="snakevision_safe_moves",
        description=(
            "列出当前局面下不会立刻撞死的所有移动方向及其存活评估。"
            "返回每个方向的名称、是否直通食物、以及预估存活率。"
            "当需要评估当前局面有哪些可行解、或比较不同动作的风险时使用。"
        ),
        properties={"state": {"type": "object", "description": "当前状态快照"}},
    )
    def snakevision_safe_moves(state: dict[str, Any]) -> dict[str, Any]:
        game = _restore(state)
        moves = []
        for d in safe_directions(game):
            probe = game.copy()
            probe.step(d)
            moves.append({
                "direction": d.name,
                "reaches_food": probe.food is not None
                and probe.snake.head == probe.food,
                "survival": round(survival_probability(probe), 3),
            })
        return {
            "safe_count": len(moves),
            "recommended": (
                plan_direction(game, try_hard=True).name
                if safe_directions(game) else None
            ),
            "moves": moves,
        }

    @registry.tool(
        name="snakevision_render",
        description=(
            "把对局状态渲染成 ASCII 字符画，可选颜色与坐标轴。"
            "返回纯文本，不做任何终端 IO。当需要在回复中直观展示网格局面、"
            "或把渲染结果写入文件时使用。"
        ),
        properties={
            "state": {"type": "object", "description": "当前状态快照"},
            "style": {
                "type": "string",
                "enum": ["plain", "ascii", "blocks"],
                "default": "ascii",
                "description": "plain=无色纯 ASCII，ascii=默认，blocks=方块风格",
            },
            "hud": {
                "type": ["boolean", "null"],
                "default": True,
                "description": "是否显示底部状态栏",
            },
            "show_grid": {
                "type": ["boolean", "null"],
                "default": False,
                "description": "是否显示坐标轴",
            },
        },
    )
    def snakevision_render(
        state: dict[str, Any],
        style: str = "ascii",
        hud: bool | None = None,
        show_grid: bool | None = None,
    ) -> dict[str, Any]:
        game = _restore(state)
        themes = {
            "plain": Theme.ascii_plain(),
            "ascii": Theme(),
            "blocks": Theme.blocks(),
        }
        text = render_once(
            game,
            theme=themes.get(style, Theme()),
            hud=True if hud is None else bool(hud),
            show_grid=bool(show_grid),
        )
        return {"frame": text, "lines": text.count("\n") + 1}


def _restore(state: dict[str, Any]) -> Game:
    """从快照还原 Game 对象。

    快照是纯 JSON，agent 可以持有它并在多轮对话间传递；
    这里委托给 `Game.restore` 做带防御性解析的还原。
    """
    return Game.restore(state)


__all__ = ["register"]
