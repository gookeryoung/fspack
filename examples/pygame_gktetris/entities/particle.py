"""GK Tetris 实体模块 - 包含粒子效果等游戏实体 (pygame 版)"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from ..conf import CELL_SIZE, COLS, PIECE_COLORS

if TYPE_CHECKING:
    import pygame as pg


class Particle:
    def __init__(self, x: float, y: float, color: tuple[int, int, int]) -> None:
        self.x = x
        self.y = y
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(2, 8)
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed - random.uniform(1, 4)
        self.color = color
        self.life = random.uniform(0.5, 1.5)
        self.max_life = self.life
        self.size = random.uniform(2, 6)

    def update(self, dt: float) -> bool:
        self.x += self.vx * dt * 60
        self.y += self.vy * dt * 60
        self.vy += 0.15 * dt * 60
        self.life -= dt
        return self.life > 0

    def draw(self, surface: pg.Surface) -> None:
        import pygame

        alpha = max(0.0, self.life / self.max_life)
        size = max(1, int(self.size * alpha))
        # 将 alpha 衰减映射到颜色亮度，模拟透明度
        r = min(255, int(self.color[0] * alpha + 255 * (1 - alpha)))
        g = min(255, int(self.color[1] * alpha + 255 * (1 - alpha)))
        b = min(255, int(self.color[2] * alpha + 255 * (1 - alpha)))
        pygame.draw.circle(surface, (r, g, b), (int(self.x), int(self.y)), size)


class ParticleSystem:
    def __init__(self) -> None:
        self.particles: list[Particle] = []
        self.max_particles = 300

    def emit_row(
        self,
        row: int,
        board_offset_x: int,
        board_offset_y: int,
        board: list[list[str | None]],
        rows_count: int = 20,
    ) -> None:
        for col in range(COLS):
            cell = board[row][col]
            if cell:
                # pygame Y 轴向下，直接使用 row * CELL_SIZE
                cx = board_offset_x + col * CELL_SIZE + CELL_SIZE // 2
                cy = board_offset_y + row * CELL_SIZE + CELL_SIZE // 2
                color = PIECE_COLORS.get(cell, ((255, 255, 255),) * 4)[0]
                for _ in range(5):
                    if len(self.particles) >= self.max_particles:
                        break
                    self.particles.append(Particle(cx, cy, color))

    def update(self, dt: float) -> None:
        self.particles = [p for p in self.particles if p.update(dt)]

    def draw(self, surface: pg.Surface) -> None:
        for p in self.particles:
            p.draw(surface)
