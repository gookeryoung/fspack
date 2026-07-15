"""Animation module for entities (pygame 版)."""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ..conf import BOARD_WIDTH, CELL_SIZE, COLS, PIECE_COLORS

if TYPE_CHECKING:
    import pygame as pg


class BaseAnimation(ABC):
    active: bool

    def __init__(self) -> None:
        self.timer: float = 0.0
        self.active = True

    def update(self, dt: float) -> None:
        self.timer += dt

    @abstractmethod
    def draw(self, surface: pg.Surface, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError


class LineClearAnimation(BaseAnimation, ABC):
    ANIM_PARAMS: dict[str, dict[str, Any]] = {
        "SINGLE": {
            "dur": 0.5,
            "flash_col": (255, 255, 255),
            "flash_a": 180,
            "shrink": True,
            "shake_i": 2,
            "shake_dur": 0.15,
            "particle_n": 6,
            "particle_s": (2, 5),
            "wave": False,
            "screen_flash": False,
            "color_burst": False,
            "extra_glow": False,
        },
        "DOUBLE": {
            "dur": 0.55,
            "flash_col": (255, 220, 100),
            "flash_a": 220,
            "shrink": True,
            "shake_i": 4,
            "shake_dur": 0.2,
            "particle_n": 10,
            "particle_s": (3, 6),
            "wave": False,
            "screen_flash": False,
            "color_burst": False,
            "extra_glow": False,
        },
        "TRIPLE": {
            "dur": 0.65,
            "flash_col": (255, 120, 255),
            "flash_a": 255,
            "shrink": True,
            "shake_i": 6,
            "shake_dur": 0.25,
            "particle_n": 14,
            "particle_s": (3, 7),
            "wave": True,
            "screen_flash": True,
            "color_burst": True,
            "extra_glow": False,
        },
        "TETRIS": {
            "dur": 0.75,
            "flash_col": (255, 200, 50),
            "flash_a": 255,
            "shrink": False,
            "shake_i": 10,
            "shake_dur": 0.35,
            "particle_n": 20,
            "particle_s": (4, 8),
            "wave": True,
            "screen_flash": True,
            "color_burst": True,
            "extra_glow": True,
        },
        "TSPIN": {
            "dur": 0.7,
            "flash_col": (180, 80, 255),
            "flash_a": 255,
            "shrink": True,
            "shake_i": 6,
            "shake_dur": 0.3,
            "particle_n": 16,
            "particle_s": (3, 7),
            "wave": True,
            "screen_flash": True,
            "color_burst": True,
            "extra_glow": False,
        },
        "TSPIN_MINI": {
            "dur": 0.5,
            "flash_col": (160, 120, 255),
            "flash_a": 180,
            "shrink": True,
            "shake_i": 3,
            "shake_dur": 0.2,
            "particle_n": 10,
            "particle_s": (2, 5),
            "wave": False,
            "screen_flash": True,
            "color_burst": False,
            "extra_glow": False,
        },
        "PERFECT": {
            "dur": 1.0,
            "flash_col": (255, 220, 50),
            "flash_a": 255,
            "shrink": False,
            "shake_i": 14,
            "shake_dur": 0.5,
            "particle_n": 30,
            "particle_s": (4, 10),
            "wave": True,
            "screen_flash": True,
            "color_burst": True,
            "extra_glow": True,
        },
        "B2B_TETRIS": {
            "dur": 0.8,
            "flash_col": (255, 150, 50),
            "flash_a": 255,
            "shrink": False,
            "shake_i": 12,
            "shake_dur": 0.4,
            "particle_n": 24,
            "particle_s": (4, 9),
            "wave": True,
            "screen_flash": True,
            "color_burst": True,
            "extra_glow": True,
        },
    }

    def __init__(
        self,
        rows: list[int],
        anim_type: str = "SINGLE",
        board: list[list[str | None]] | None = None,
    ) -> None:
        super().__init__()
        self.rows: list[int] = rows
        self.n: int = len(rows)
        self.anim_type: str = anim_type
        self.board: list[list[str | None]] | None = board
        self.params: dict[str, Any] = self.ANIM_PARAMS.get(
            anim_type,
            self.ANIM_PARAMS["SINGLE"],
        )
        self.duration: float = self.params["dur"]

    def update(self, dt: float) -> None:
        super().update(dt)
        if self.timer >= self.duration:
            self.active = False

    @staticmethod
    def _draw_cell(
        surface: pg.Surface,
        x: int,
        y: int,
        colors: tuple[tuple[int, int, int], ...],
        brightness: float = 1.0,
        w: int = CELL_SIZE,
        h: int = CELL_SIZE,
    ) -> None:
        import pygame

        main, light, dark, _ = colors
        r: int = min(255, int(main[0] * brightness))
        g: int = min(255, int(main[1] * brightness))
        b: int = min(255, int(main[2] * brightness))
        pygame.draw.rect(surface, (r, g, b), (x, y, w, h))
        lr: int = min(255, int(light[0] * brightness))
        lg: int = min(255, int(light[1] * brightness))
        lb: int = min(255, int(light[2] * brightness))
        dr: int = min(255, int(dark[0] * brightness))
        dg: int = min(255, int(dark[1] * brightness))
        db: int = min(255, int(dark[2] * brightness))
        pygame.draw.line(surface, (lr, lg, lb), (x, y), (x + w, y), 2)
        pygame.draw.line(surface, (lr, lg, lb), (x, y), (x, y + h), 2)
        pygame.draw.line(surface, (dr, dg, db), (x, y + h), (x + w, y + h), 2)
        pygame.draw.line(surface, (dr, dg, db), (x + w, y), (x + w, y + h), 2)

    def _draw_flash(
        self,
        surface: pg.Surface,
        bx: int,
        by: int,
        progress: float,
        flash_end: float,
    ) -> None:
        import pygame

        if progress < flash_end:
            alpha: int = int(self.params["flash_a"] * (progress / flash_end))
            for row in self.rows:
                # pygame Y 轴向下，直接使用 row * CELL_SIZE
                ry = by + row * CELL_SIZE
                flash_surf = pygame.Surface((BOARD_WIDTH, CELL_SIZE), pygame.SRCALPHA)
                flash_surf.fill((*self.params["flash_col"], alpha))
                surface.blit(flash_surf, (bx, ry))


class ShrinkLineClear(LineClearAnimation):
    def draw(self, surface: pg.Surface, bx: int = 0, by: int = 0) -> None:
        if not self.active:
            return
        progress: float = self.timer / self.duration
        self._draw_flash(surface, bx, by, progress, 0.25)
        if 0.25 <= progress < 1.0:
            shrink: float = 1.0 - (progress - 0.25) / 0.75
            for row in self.rows:
                for col in range(COLS):
                    cell: str | None = self.board[row][col] if self.board else None
                    if cell:
                        cx: int = bx + col * CELL_SIZE + CELL_SIZE // 2
                        cy: int = by + row * CELL_SIZE + CELL_SIZE // 2
                        w: int = max(1, int(CELL_SIZE * shrink))
                        h: int = max(1, int(CELL_SIZE * shrink))
                        colors = PIECE_COLORS.get(cell, ((200, 200, 200),) * 4)
                        self._draw_cell(
                            surface,
                            cx - w // 2,
                            cy - h // 2,
                            colors,
                            0.7,
                            w,
                            h,
                        )
        if self.params["wave"]:
            self._draw_wave(surface, bx, by, progress)
        if self.params["extra_glow"] and progress < 0.4:
            self._draw_glow(surface, bx, by, progress)

    def _draw_wave(
        self,
        surface: pg.Surface,
        bx: int,
        by: int,
        progress: float,
    ) -> None:
        import pygame

        wave_peak: float = 0.3
        if progress > wave_peak:
            return
        wave_val: float = math.sin(progress / wave_peak * math.pi)
        amplitude: float = CELL_SIZE * 0.4 * wave_val
        for row in self.rows:
            for col in range(COLS):
                cx: int = bx + col * CELL_SIZE + CELL_SIZE // 2
                cy: int = by + row * CELL_SIZE + CELL_SIZE // 2
                offset: int = int(amplitude * math.sin(col * 0.9 + progress * 15))
                if abs(offset) > 2:
                    pygame.draw.circle(
                        surface,
                        self.params["flash_col"],
                        (cx + offset, cy),
                        max(1, int(3 * wave_val)),
                    )

    def _draw_glow(
        self,
        surface: pg.Surface,
        bx: int,
        by: int,
        progress: float,
    ) -> None:
        import pygame

        glow_intensity: float = (1.0 - progress / 0.4) * 0.6
        for row in self.rows:
            ry = by + row * CELL_SIZE
            glow_surf = pygame.Surface(
                (BOARD_WIDTH + 20, CELL_SIZE + 20),
                pygame.SRCALPHA,
            )
            glow_surf.fill((*self.params["flash_col"], int(100 * glow_intensity)))
            surface.blit(glow_surf, (bx - 10, ry - 10))


class DissolveLineClear(LineClearAnimation):
    def draw(self, surface: pg.Surface, bx: int = 0, by: int = 0) -> None:
        if not self.active:
            return
        progress: float = self.timer / self.duration
        self._draw_flash(surface, bx, by, progress, 0.15)
        if 0.15 <= progress < 1.0:
            t: float = (progress - 0.15) / 0.85
            for row in self.rows:
                for col in range(COLS):
                    cell: str | None = self.board[row][col] if self.board else None
                    if cell:
                        cx: int = bx + col * CELL_SIZE + CELL_SIZE // 2
                        cy: int = by + row * CELL_SIZE + CELL_SIZE // 2
                        offset_y: int = int(t * CELL_SIZE * 1.5)
                        wave: int = int(
                            math.sin(col * 0.8 + t * 8) * CELL_SIZE * 0.4 * t,
                        )
                        cx_shake: int = int(
                            math.cos(t * 10 + col) * CELL_SIZE * 0.3 * t,
                        )
                        w: int = max(1, int(CELL_SIZE * (1 - t * 0.7)))
                        h: int = max(1, int(CELL_SIZE * (1 - t * 0.3)))
                        colors = PIECE_COLORS.get(cell, ((200, 200, 200),) * 4)
                        self._draw_cell(
                            surface,
                            cx - w // 2 + cx_shake,
                            cy - h // 2 + offset_y + wave,
                            colors,
                            1.0 - t * 0.6,
                            w,
                            h,
                        )
        if self.params["wave"]:
            self._draw_wave(surface, bx, by, progress)
        if self.params["extra_glow"] and progress < 0.4:
            self._draw_glow(surface, bx, by, progress)

    def _draw_wave(
        self,
        surface: pg.Surface,
        bx: int,
        by: int,
        progress: float,
    ) -> None:
        import pygame

        wave_peak: float = 0.3
        if progress > wave_peak:
            return
        wave_val: float = math.sin(progress / wave_peak * math.pi)
        amplitude: float = CELL_SIZE * 0.4 * wave_val
        for row in self.rows:
            for col in range(COLS):
                cx: int = bx + col * CELL_SIZE + CELL_SIZE // 2
                cy: int = by + row * CELL_SIZE + CELL_SIZE // 2
                offset: int = int(amplitude * math.sin(col * 0.9 + progress * 15))
                if abs(offset) > 2:
                    pygame.draw.circle(
                        surface,
                        self.params["flash_col"],
                        (cx + offset, cy),
                        max(1, int(3 * wave_val)),
                    )

    def _draw_glow(
        self,
        surface: pg.Surface,
        bx: int,
        by: int,
        progress: float,
    ) -> None:
        import pygame

        glow_intensity: float = (1.0 - progress / 0.4) * 0.6
        for row in self.rows:
            ry = by + row * CELL_SIZE
            glow_surf = pygame.Surface(
                (BOARD_WIDTH + 20, CELL_SIZE + 20),
                pygame.SRCALPHA,
            )
            glow_surf.fill((*self.params["flash_col"], int(100 * glow_intensity)))
            surface.blit(glow_surf, (bx - 10, ry - 10))


def create_line_clear_animation(
    rows: list[int],
    anim_type: str = "SINGLE",
    board: list[list[str | None]] | None = None,
) -> LineClearAnimation:
    params: dict[str, Any] = LineClearAnimation.ANIM_PARAMS.get(
        anim_type,
        LineClearAnimation.ANIM_PARAMS["SINGLE"],
    )
    if params["shrink"]:
        return ShrinkLineClear(rows, anim_type, board)
    else:
        return DissolveLineClear(rows, anim_type, board)


class ShockwaveAnimation(BaseAnimation):
    def __init__(
        self,
        center_y: float,
        color: tuple[int, int, int] = (255, 200, 50),
    ) -> None:
        super().__init__()
        self.center_y: float = center_y
        self.color: tuple[int, int, int] = color
        self.duration: float = 0.4

    def draw(self, surface: pg.Surface, bx: int = 0, by: int = 0) -> None:
        import pygame

        if not self.active:
            return
        t: float = self.timer / self.duration
        radius: int = int(BOARD_WIDTH * (0.5 + t * 1.5))
        alpha: int = int(200 * (1 - t))
        if alpha <= 0:
            return
        cy: int = by + int(self.center_y)
        # 使用临时 surface 绘制带 alpha 的椭圆
        size = radius * 2 + 20
        temp = pygame.Surface((size, size), pygame.SRCALPHA)
        thickness = max(1, int(6 * (1 - t)))
        pygame.draw.ellipse(temp, (*self.color, alpha), (0, 0, size, size), thickness)
        surface.blit(temp, (bx + BOARD_WIDTH // 2 - size // 2, cy - size // 2))


class ColorBurstAnimation(BaseAnimation):
    def __init__(self, rows: list[int], board: list[list[str | None]]) -> None:
        super().__init__()
        self.particles: list[dict[str, Any]] = []
        self.duration: float = 0.8
        hues: list[int] = [0, 60, 120, 180, 240, 300]
        for row in rows:
            for col in range(COLS):
                if board[row][col]:
                    cx: int = col * CELL_SIZE + CELL_SIZE // 2
                    cy: int = row * CELL_SIZE + CELL_SIZE // 2
                    for _ in range(3):
                        angle: float = random.uniform(0, 2 * math.pi)
                        speed: float = random.uniform(3, 10)
                        hue: int = random.choice(hues)
                        r: int = int(128 + 127 * math.sin(hue * math.pi / 180))
                        g: int = int(128 + 127 * math.sin((hue + 120) * math.pi / 180))
                        b: int = int(128 + 127 * math.sin((hue + 240) * math.pi / 180))
                        self.particles.append(
                            {
                                "x": float(cx),
                                "y": float(cy),
                                "vx": math.cos(angle) * speed,
                                "vy": math.sin(angle) * speed - 3,
                                "color": (r, g, b),
                                "life": random.uniform(0.4, 0.8),
                                "max_life": 0.8,
                                "size": random.uniform(2, 5),
                            },
                        )

    def update(self, dt: float) -> None:
        super().update(dt)
        for p in self.particles:
            p["x"] += p["vx"] * dt * 60
            p["y"] += p["vy"] * dt * 60
            p["vy"] = p["vy"] + 0.2 * dt * 60
            p["life"] = p["life"] - dt
        self.particles = [p for p in self.particles if p["life"] > 0]
        if self.timer >= self.duration and not self.particles:
            self.active = False

    def draw(self, surface: pg.Surface, bx: int = 0, by: int = 0) -> None:
        import pygame

        for p in self.particles:
            alpha: float = max(0, p["life"] / p["max_life"])
            size: int = max(1, int(p["size"] * alpha))
            c: tuple[int, int, int] = p["color"]
            pygame.draw.circle(surface, c, (bx + int(p["x"]), by + int(p["y"])), size)


class ScreenFlashAnimation(BaseAnimation):
    def __init__(
        self,
        color: tuple[int, int, int] = (255, 255, 255),
        duration: float = 0.15,
        intensity: float = 0.4,
    ) -> None:
        super().__init__()
        self.color: tuple[int, int, int] = color
        self.duration: float = duration
        self.intensity: float = intensity

    def draw(self, surface: pg.Surface, _width: int = 0, _height: int = 0) -> None:
        import pygame

        if not self.active:
            return
        t: float = self.timer / self.duration
        alpha: int = (
            int(255 * self.intensity * (t / 0.3)) if t < 0.3 else int(255 * self.intensity * (1 - (t - 0.3) / 0.7))
        )
        if alpha <= 0:
            return
        w = surface.get_width()
        h = surface.get_height()
        flash_surf = pygame.Surface((w, h), pygame.SRCALPHA)
        flash_surf.fill((*self.color, alpha))
        surface.blit(flash_surf, (0, 0))
