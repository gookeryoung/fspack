"""$project_name 入口：Pygame 游戏骨架示例."""

import sys

import pygame


def main() -> None:
    """启动 Pygame 游戏窗口（ESC 或关闭窗口退出）."""
    pygame.init()
    screen = pygame.display.set_mode((640, 480))
    pygame.display.set_caption("$project_name")
    clock = pygame.time.Clock()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        screen.fill((30, 30, 30))
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
