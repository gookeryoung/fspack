"""$project_name 入口：贪吃蛇完整游戏."""

import random
import sys

import pygame

CELL = 20
COLS, ROWS = 32, 24
WIDTH, HEIGHT = COLS * CELL, ROWS * CELL


def _spawn_food(snake: list[tuple[int, int]]) -> tuple[int, int]:
    """随机生成食物位置（避开蛇身）."""
    while True:
        food = (random.randint(0, COLS - 1), random.randint(0, ROWS - 1))
        if food not in snake:
            return food


def main() -> None:
    """启动贪吃蛇游戏（方向键控制，ESC 退出）."""
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("$project_name - 贪吃蛇")
    clock = pygame.time.Clock()

    snake = [(COLS // 2, ROWS // 2)]
    direction = (1, 0)
    food = _spawn_food(snake)
    score = 0

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_UP and direction != (0, 1):
                    direction = (0, -1)
                elif event.key == pygame.K_DOWN and direction != (0, -1):
                    direction = (0, 1)
                elif event.key == pygame.K_LEFT and direction != (1, 0):
                    direction = (-1, 0)
                elif event.key == pygame.K_RIGHT and direction != (-1, 0):
                    direction = (1, 0)

        head = (snake[0][0] + direction[0], snake[0][1] + direction[1])
        if head[0] < 0 or head[0] >= COLS or head[1] < 0 or head[1] >= ROWS or head in snake:
            running = False
            continue

        snake.insert(0, head)
        if head == food:
            score += 10
            food = _spawn_food(snake)
        else:
            snake.pop()

        screen.fill((15, 15, 15))
        for seg in snake:
            pygame.draw.rect(screen, (50, 200, 50), (seg[0] * CELL, seg[1] * CELL, CELL, CELL))
        pygame.draw.rect(screen, (200, 50, 50), (food[0] * CELL, food[1] * CELL, CELL, CELL))
        pygame.display.flip()
        clock.tick(10)

    pygame.quit()
    print(f"游戏结束，得分: {score}")
    sys.exit(0)


if __name__ == "__main__":
    main()
