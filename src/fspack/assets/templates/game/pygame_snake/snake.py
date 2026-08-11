"""pygame 贪吃蛇示例：真实窗口 + 网格 + 键盘控制。

验证 pygame 在 embed python 下打包可用。测试时设置 SDL_VIDEODRIVER=dummy 可在无显示环境运行。
"""

from __future__ import annotations

import os
import random

CELL = 20
COLS = 30
ROWS = 20
WIDTH = CELL * COLS
HEIGHT = CELL * ROWS
FPS = 10
DUMMY_MAX_FRAMES = 30

_BG = (0, 0, 0)
_BORDER = (64, 64, 64)
_FOOD = (255, 0, 0)
_SNAKE_HEAD = (0, 255, 0)
_SNAKE_BODY = (0, 180, 0)
_TEXT = (255, 255, 255)


def main() -> None:
    """运行贪吃蛇游戏：方向键控制，吃食物增长，撞墙或自身结束."""
    import pygame

    is_dummy = os.environ.get("SDL_VIDEODRIVER") == "dummy"

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Snake")
    clock = pygame.time.Clock()
    font = pygame.font.Font(None, 24)

    key_dirs = {
        pygame.K_UP: (0, -1),
        pygame.K_DOWN: (0, 1),
        pygame.K_LEFT: (-1, 0),
        pygame.K_RIGHT: (1, 0),
    }

    snake: list[tuple[int, int]] = [(COLS // 2, ROWS // 2)]
    direction = (1, 0)
    food = _spawn_food(snake)
    score = 0
    frame = 0

    print("snake ready")

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return
            if event.type == pygame.KEYDOWN:
                new_dir = key_dirs.get(event.key)
                if new_dir and new_dir != (-direction[0], -direction[1]):
                    direction = new_dir

        head = snake[0]
        new_head = (head[0] + direction[0], head[1] + direction[1])
        if new_head[0] < 0 or new_head[0] >= COLS or new_head[1] < 0 or new_head[1] >= ROWS or new_head in snake:
            print(f"game over, score: {score}")
            pygame.quit()
            return

        snake.insert(0, new_head)
        if new_head == food:
            score += 1
            food = _spawn_food(snake)
        else:
            snake.pop()

        screen.fill(_BG)
        pygame.draw.rect(screen, _FOOD, (food[0] * CELL, food[1] * CELL, CELL, CELL))
        for i, (x, y) in enumerate(snake):
            color = _SNAKE_HEAD if i == 0 else _SNAKE_BODY
            pygame.draw.rect(screen, color, (x * CELL, y * CELL, CELL, CELL))
        pygame.draw.rect(screen, _BORDER, (0, 0, WIDTH, HEIGHT), 1)
        text = font.render(f"Score: {score}", True, _TEXT)
        screen.blit(text, (4, 4))
        pygame.display.flip()

        frame += 1
        if is_dummy and frame >= DUMMY_MAX_FRAMES:
            pygame.quit()
            return

        clock.tick(FPS)


def _spawn_food(snake: list[tuple[int, int]]) -> tuple[int, int]:
    """在非蛇身格子随机生成食物."""
    while True:
        pos = (random.randint(0, COLS - 1), random.randint(0, ROWS - 1))
        if pos not in snake:
            return pos


if __name__ == "__main__":
    main()
