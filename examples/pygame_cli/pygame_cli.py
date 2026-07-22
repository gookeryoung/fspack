"""有库 pygame 示例：init + 打印版本."""


def main() -> None:
    """初始化 pygame 并打印版本."""
    import pygame

    pygame.init()
    print(f"pygame {pygame.version.ver}")
    pygame.quit()


if __name__ == "__main__":
    main()
