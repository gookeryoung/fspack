"""有库 pygame 示例：初始化 + dummy 驱动画蛇头。

验证 pygame 在 embed python 下打包可用，使用 dummy 视频驱动避免依赖显示设备。
"""

import os


def main() -> None:
    """初始化 pygame dummy 驱动，画一帧蛇头方块。."""
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame

    pygame.init()
    screen = pygame.display.set_mode((200, 200))
    screen.fill((0, 0, 0))
    pygame.draw.rect(screen, (0, 255, 0), (100, 100, 20, 20))
    pygame.display.flip()
    print("snake ready")
    pygame.quit()


if __name__ == "__main__":
    main()
