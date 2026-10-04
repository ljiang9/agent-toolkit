"""游戏状态机：蛇、食物、碰撞与生长。

对齐原项目 snake.h / snake.c 的状态设计（head/tail 双向链表 + direction +
grow + collision），但用 Python 的 list 表示身体，并显式区分「存活」与「结束原因」。

相比原项目的一处刻意简化：身体用 head→tail 顺序的 list 表示，
移动时在头部插入、尾部弹出，不需要双向链表——Python 里 list 的两端操作
都是 O(1)，双向链表的优势在这个规模下体现不出来。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .grid import Direction, Grid, Point


class Status(Enum):
    """对局状态。"""

    RUNNING = "running"
    WON = "won"          # 棋盘被填满
    DIED = "died"        # 撞墙或撞自己
    PAUSED = "paused"


@dataclass
class Snake:
    """蛇的身体与状态。

    body[0] 是头，body[-1] 是尾。
    """

    body: list[Point] = field(default_factory=list)
    direction: Direction = Direction.EAST
    pending_direction: Direction | None = None
    grow_pending: int = 0   # 还要生长几格（吃到食物后按倍率延迟生长）

    @property
    def head(self) -> Point:
        return self.body[0]

    @property
    def tail(self) -> Point:
        return self.body[-1]

    def __len__(self) -> int:
        return len(self.body)

    @classmethod
    def create(cls, x: int, y: int, direction: Direction, length: int = 3) -> "Snake":
        """创建一条蛇，头在 (x, y)，身体沿**移动方向的反方向**向后铺开。

        body[0] 是头、body[-1] 是尾，所以从 (x, y) 沿反方向依次排下去即可，
        不需要再 reverse（reverse 会把头尾颠倒，导致移动方向反了）。
        """
        dx, dy = direction.opposite.delta
        body = [Point(x + dx * i, y + dy * i) for i in range(length)]
        return cls(body=body, direction=direction)

    def copy(self) -> "Snake":
        """深拷贝。自动驾驶的「try hard」需要大量试算状态，必须隔离副本。"""
        return Snake(
            body=list(self.body),
            direction=self.direction,
            pending_direction=self.pending_direction,
            grow_pending=self.grow_pending,
        )

    def occupied(self, *, ignore_tail: bool = False) -> set[Point]:
        """身体占据的格子集合。

        ignore_tail=True 时排除尾巴——原项目 checkBody 的常见取舍：
        尾巴这一步会移走，所以它并不总是障碍。
        """
        cells = set(self.body[:-1]) if ignore_tail else set(self.body)
        return cells

    def set_direction(self, direction: Direction) -> bool:
        """设置方向。禁止直接反向（那会立刻撞上自己的脖子）。"""
        if direction is self.direction:
            return True
        if direction is self.direction.opposite:
            return False
        self.pending_direction = direction
        return True


@dataclass
class Game:
    """一局游戏的状态。"""

    grid: Grid
    snake: Snake
    food: Point | None = None
    status: Status = Status.RUNNING
    score: int = 0
    steps: int = 0
    # 因撞上自己而死亡的次数（try hard 模式下用于评估决策）
    deaths: int = 0

    @classmethod
    def create(
        cls,
        width: int = 30,
        height: int = 15,
        *,
        snake_length: int = 3,
        seed: int | None = None,
    ) -> "Game":
        grid = Grid(width, height)
        head = Point(width // 2, height // 2)
        snake = Snake.create(head.x, head.y, Direction.EAST, snake_length)
        game = cls(grid=grid, snake=snake)
        game.place_food(rng=random.Random(seed) if seed is not None else random)
        return game

    # ---------- 食物 ----------

    def place_food(self, *, rng: random.Random | None = None) -> bool:
        """在空格子中随机放食物。棋盘满时返回 False（触发胜利）。"""
        generator = rng or random
        occupied = self.snake.occupied()
        candidates = [
            p for p in self.grid.free_cells()
            if p not in occupied and p is not self.food
        ]
        if not candidates:
            self.food = None
            self.status = Status.WON
            return False
        self.food = generator.choice(candidates)
        return True

    # ---------- 状态推进 ----------

    def step(self, direction: Direction | None = None) -> Status:
        """推进一个 tick。direction 非空时先尝试转向。"""
        if self.status is not Status.RUNNING:
            return self.status

        if direction is not None:
            self.snake.set_direction(direction)
        if self.snake.pending_direction is not None:
            self.snake.direction = self.snake.pending_direction
            self.snake.pending_direction = None

        new_head = self.snake.head.move(self.snake.direction)

        # 碰撞判定：撞墙
        if self.grid.is_wall(new_head.x, new_head.y):
            self.status = Status.DIED
            return self.status

        # 撞自己。注意尾巴的特例：若这一步尾巴会移走，
        # 则「进入尾巴所在格」不算撞——这是经典贪心蛇必须处理的细节。
        growing = self.snake.grow_pending > 0 or new_head == self.food
        body_cells = self.snake.occupied(ignore_tail=not growing)
        if new_head in body_cells:
            self.status = Status.DIED
            self.deaths += 1
            return self.status

        # 移动：头插入，尾按需弹出
        self.snake.body.insert(0, new_head)
        if self.snake.grow_pending > 0:
            self.snake.grow_pending -= 1
        elif not growing:
            self.snake.body.pop()

        # 吃到食物
        if new_head == self.food:
            self.score += 1
            # 延迟 3 步再生长，避免连续吃食物时瞬间自撞
            self.snake.grow_pending = 3
            self.place_food()

        self.steps += 1
        return self.status

    # ---------- 快照 ----------

    def snapshot(self) -> dict[str, object]:
        """把当前状态转成可 JSON 序列化的快照（供 agent 逐步回放）。

        快照是自包含的：带上网格尺寸与墙信息，才能被 `_restore` 完整还原。
        """
        return {
            "status": self.status.value,
            "score": self.score,
            "steps": self.steps,
            "length": len(self.snake),
            "width": self.grid.width,
            "height": self.grid.height,
            "walls": [
                {"x": x, "y": y} for x, y in sorted(self.grid.wall_positions())
            ],
            "head": {"x": self.snake.head.x, "y": self.snake.head.y},
            "direction": self.snake.direction.name,
            "food": (
                {"x": self.food.x, "y": self.food.y} if self.food is not None else None
            ),
            "body": [{"x": p.x, "y": p.y} for p in self.snake.body],
        }

    @classmethod
    def restore(cls, snapshot: dict[str, object]) -> "Game":
        """从快照还原对局。

        快照可能来自 LLM 或跨会话保存，因此要做防御性解析——
        字段缺失或类型不符时给出明确错误，而不是静默产生错误状态。
        """
        try:
            body = [Point(int(p["x"]), int(p["y"])) for p in snapshot["body"]]  # type: ignore[index]
            width = int(snapshot.get("width") or 0)  # type: ignore[arg-type]
            height = int(snapshot.get("height") or 0)  # type: ignore[arg-type]
            if not body:
                raise ValueError("body 不能为空")
            if width <= 0 or height <= 0:
                # 尺寸缺失时按身体范围推断，保证还原仍可用
                width = max(p.x for p in body) + 1
                height = max(p.y for p in body) + 1
            grid = Grid(max(3, width), max(3, height))
            for w in snapshot.get("walls") or []:  # type: ignore[union-attr]
                grid.add_wall(int(w["x"]), int(w["y"]))
            snake = Snake(
                body=body,
                direction=Direction[str(snapshot.get("direction", "EAST"))],  # type: ignore[index]
            )
            food_raw = snapshot.get("food")
            food = (
                Point(int(food_raw["x"]), int(food_raw["y"]))  # type: ignore[index]
                if food_raw
                else None
            )
            return cls(
                grid=grid,
                snake=snake,
                food=food,
                status=Status(str(snapshot.get("status", "running"))),
                score=int(snapshot.get("score", 0)),  # type: ignore[arg-type]
                steps=int(snapshot.get("steps", 0)),  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"无法从快照还原对局：{exc}") from exc

    def copy(self) -> "Game":
        """状态深拷贝，用于自动规划的试算，不污染真实对局。"""
        return Game(
            grid=self.grid,
            snake=self.snake.copy(),
            food=self.food,
            status=self.status,
            score=self.score,
            steps=self.steps,
            deaths=self.deaths,
        )
