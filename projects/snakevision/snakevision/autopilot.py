"""自动驾驶：自动寻路并保证不死。

复刻原项目 autopilot.c 的核心思想——用寻路算法算出下一步，但**贪心吃食物
会把自己逼进死角**，所以需要额外的安全检查。

三级策略（对应原项目的 try-hard 参数）：

1. **贴尾法**：能安全走到食物就走。
2. **A* 兜底**：贴尾法失败时，先保证下一步是安全的（能继续移动），
   而不是直奔食物。
3. **试算法（try_hard）**：对候选动作做多步模拟，挑一个存活步数最多的。
   这是最稳但最慢的档位。
"""

from __future__ import annotations

from .game import Game, Snake, Status
from .grid import Direction, Grid, Point
from .pathfind import bfs_path, find_path

# 候选方向顺序：优先保持当前方向，减少无谓转向
_CANDIDATE_ORDER = {
    Direction.NORTH: (Direction.NORTH, Direction.EAST, Direction.WEST, Direction.SOUTH),
    Direction.SOUTH: (Direction.SOUTH, Direction.EAST, Direction.WEST, Direction.NORTH),
    Direction.EAST: (Direction.EAST, Direction.NORTH, Direction.SOUTH, Direction.WEST),
    Direction.WEST: (Direction.WEST, Direction.NORTH, Direction.SOUTH, Direction.EAST),
}


def safe_directions(game: Game) -> list[Direction]:
    """当前局面下不会立刻撞死的方向。

    判据：下一步不撞墙、不撞身体（尾巴会移走的情况要排除）。
    """
    out = []
    for direction in _CANDIDATE_ORDER[game.snake.direction]:
        if direction is game.snake.direction.opposite and len(game.snake) > 1:
            continue  # 反向直接撞脖子
        nxt = game.snake.head.move(direction)
        if game.grid.is_wall(nxt.x, nxt.y):
            continue
        if nxt in game.snake.occupied(ignore_tail=True):
            continue
        out.append(direction)
    return out


def _would_survive(game: Game, direction: Direction, food_seeking: bool) -> bool:
    """走这一步后是否还能继续生存（至少不立刻死，且食物可达或尾巴可达）。"""
    probe = game.copy()
    probe.step(direction)
    if probe.status is not Status.RUNNING:
        return False
    if not safe_directions(probe):
        return False  # 走进死胡同，下一步必死
    if food_seeking and probe.food is not None:
        return bool(find_path(probe.grid, probe.snake.head, probe.food,
                              _target_blocked(probe)))
    return True


def _target_blocked(game: Game) -> set[Point]:
    """作为寻路障碍的身体格子。

    必须**去掉蛇头自己**（body[0]）——蛇本来就站在那儿，
    若把它算作障碍，路径搜索会因为「起点不可通行」而直接判无解。
    这正是早期版本明明有通路却返回空路径的原因。
    """
    return set(game.snake.body[1:]) - {game.snake.tail}


def plan_direction(game: Game, *, try_hard: bool = False) -> Direction | None:
    """给出下一步方向。无安全动作时返回 None（游戏即将结束）。

    核心思路：**先算出通往食物的最短路径，再取路径的第一步**，
    并逐个校验该方向是否安全。安全则走，不安全才退而求其次选别的方向。

    早期版本用「候选顺序 = 保持当前方向优先」+「只问能不能活」，
    结果是蛇会一直保持原方向绕圈、永远吃不到食物——
    因为「能活」和「能吃到食物」是两件事，必须显式朝目标走。
    """
    if game.status is not Status.RUNNING:
        options = safe_directions(game)
        return options[0] if options else None

    options = safe_directions(game)
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    if game.food is None:
        return options[0]

    # 1) 算出通往食物的路径，把「朝食物走」的方向排在最前
    path = find_path(game.grid, game.snake.head, game.food, _target_blocked(game))
    preferred: list[Direction] = []
    if path:
        nxt = path[0]
        dx, dy = nxt.x - game.snake.head.x, nxt.y - game.snake.head.y
        step_dir = {(0, -1): Direction.NORTH, (0, 1): Direction.SOUTH,
                    (1, 0): Direction.EAST, (-1, 0): Direction.WEST}.get((dx, dy))
        if step_dir is not None and step_dir in options:
            preferred.append(step_dir)
    # 其余方向按「保持当前方向优先」排，作为退路
    for d in options:
        if d not in preferred:
            preferred.append(d)

    # 2) 按优先级逐个校验：能安全朝食物走就优先走
    for direction in preferred:
        if _would_survive(game, direction, food_seeking=True):
            return direction

    # 3) 都吃不到？那就退而求其次，保证活着
    for direction in preferred:
        if _would_survive(game, direction, food_seeking=False):
            return direction

    # 4) try_hard：逐步模拟，取存活步数最多的动作
    if try_hard:
        best: tuple[int, Direction] | None = None
        for direction in preferred:
            survived = _simulate_survival(game, direction, max_steps=40)
            if best is None or survived > best[0]:
                best = (survived, direction)
        if best is not None:
            return best[1]

    return options[0]


def _simulate_survival(game: Game, first: Direction, *, max_steps: int = 40) -> int:
    """模拟若干步，返回存活步数。-1 表示第一步就死。"""
    probe = game.copy()
    direction = first
    for count in range(max_steps):
        probe.step(direction)
        if probe.status is not Status.RUNNING:
            return count - 1 if count == 0 else count
        direction = plan_direction(probe, try_hard=False)  # type: ignore[assignment]
        if direction is None:
            return count
    return max_steps


def path_to_food(game: Game, *, algorithm: str = "astar") -> list[Point]:
    """算出头到食物的路径（不考虑蛇身阻挡，仅用于可视化调试）。"""
    if game.food is None:
        return []
    return find_path(
        game.grid,
        game.snake.head,
        game.food,
        game.snake.occupied(ignore_tail=True),
        algorithm=algorithm,
    )


def run_autopilot(game: Game, *, max_steps: int | None = None,
                  try_hard: bool = False) -> Game:
    """原地跑自动驾驶，直到死亡或达到步数上限。"""
    limit = max_steps if max_steps is not None else (game.grid.width * game.grid.height)
    for _ in range(limit):
        if game.status is not Status.RUNNING:
            break
        direction = plan_direction(game, try_hard=try_hard)
        if direction is None:
            game.status = Status.DIED
            break
        game.step(direction)
    return game


def survival_probability(
    game: Game, *, try_hard: bool = False, sample_steps: int = 60
) -> float:
    """估算存活率：深拷贝后跑自动驾驶，返回存活步数 / 采样步数。

    用途是给 agent 一个可解释的风险指标，而不是让它盲目走一步看一步。
    """
    probe = game.copy()
    before = probe.steps
    run_autopilot(probe, max_steps=sample_steps, try_hard=try_hard)
    return (probe.steps - before) / sample_steps
