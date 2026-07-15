"""
GK Tetris - 现代化俄罗斯方块 (pygame 版)
基于 Pygame, 拥有丰富渐变色彩和动感消除效果
"""

from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path

import pygame

from .conf import (
    ACCENT_COLOR,
    BG_COLOR,
    BOARD_BG,
    BOARD_HEIGHT,
    BOARD_WIDTH,
    BORDER_COLOR,
    BORDER_GLOW,
    CELL_SIZE,
    COLS,
    FPS,
    GRID_COLOR,
    PIECE_COLORS,
    ROWS,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    SHAPES,
    SIDEBAR_BG,
    SIDEBAR_WIDTH,
    TEXT_COLOR,
    WALL_KICKS,
)
from .entities.animation import (
    ColorBurstAnimation,
    LineClearAnimation,
    ScreenFlashAnimation,
    ShockwaveAnimation,
    create_line_clear_animation,
)
from .entities.particle import ParticleSystem
from .entities.piece import Piece, PieceGenerator


class TetrisGame:
    def __init__(self) -> None:
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("GK Tetris - 现代俄罗斯方块")
        self.clock = pygame.time.Clock()
        self.running = True

        # 字体初始化 - 使用 Font() 而非 SysFont() 以避免 Windows 注册表 bug
        font_path = self._find_font_path()
        self._font_large = pygame.font.Font(font_path, 36)
        self._font_medium = pygame.font.Font(font_path, 22)
        self._font_medium_plain = pygame.font.Font(font_path, 22)
        self._font_small = pygame.font.Font(font_path, 16)
        self._font_small_bold = pygame.font.Font(font_path, 16)
        self._font_fps = pygame.font.Font(font_path, 18)
        self._font_key = pygame.font.Font(font_path, 14)

        # 预计算渐变颜色缓存
        self._gradient_cache: dict[str, list[tuple[int, int, int]]] = {}
        self._init_gradient_cache()

        self.piece_generator = PieceGenerator()
        self.preview_queue: list[str] = []

        self.board_x = 20
        self.board_y = 20  # pygame Y 轴向下，从顶部开始

        self.board: list[list[str | None]] = [[None] * COLS for _ in range(ROWS)]
        self.current_piece: Piece | None = None
        self.next_piece: Piece | None = None
        self.held_piece: Piece | None = None
        self.can_hold = True
        self.score = 0
        self.level = 1
        self.lines_cleared = 0
        self.game_over = False
        self.paused = False

        self.skill_charges = 0
        self.skill_max_charges = 3
        self.skill_active = False
        self.skill_timer = 0.0
        self.skill_flash_timer = 0.0
        self.skill_sweep_rows: list[int] = []
        self.skill_sweep_progress = 0.0
        self.skill_lightning_rows: list[int] = []
        self.skill_lightning_progress = 0.0
        self.skill_type = ""
        self.skill_laser_col = 0

        self.pending_garbage = 0
        self.garbage_rows: list[list[str | None]] = []
        self.attack_meter = 0

        self.fall_timer = 0.0
        self.fall_speed = self._get_fall_speed()
        self.lock_timer = 0.0
        self.lock_delay = 0.5
        self.is_locking = False

        self.particles = ParticleSystem()
        self.clear_animation: LineClearAnimation | None = None
        self.clearing_rows: list[int] = []
        self.shockwave: ShockwaveAnimation | None = None
        self.color_burst: ColorBurstAnimation | None = None
        self.screen_flash: ScreenFlashAnimation | None = None

        self.glow_timer = 0.0
        self.shake_timer = 0.0
        self.shake_intensity = 0.0
        self.combo_count = 0
        self.b2b_count = 0
        self.last_move_was_rotate = False
        self.tspin_trace: dict[str, bool] = {"was_kick": False}
        self.notification_text = ""
        self.notification_timer = 0.0

        self.stars: list[tuple[int, int, float, float]] = [
            (
                random.randint(0, SCREEN_WIDTH),
                random.randint(0, SCREEN_HEIGHT),
                random.uniform(0.5, 2.0),
                random.uniform(0.3, 1.0),
            )
            for _ in range(30)
        ]

        self._spawn_piece()

        self.down_pressed = False
        self.das_timer = 0.0
        self.das_direction = 0
        self.das_delay = 0.17
        self.das_repeat = 0.05
        self.das_active = False

        # FPS 监控
        self.fps = 0
        self.frame_count = 0
        self.fps_timer = 0.0
        self.fps_display_interval = 0.25

    # ─── 字体加载 ───

    @staticmethod
    def _find_font_path() -> str | None:
        """查找系统中文字体文件，跨平台兼容.

        - Windows: 搜索常见中文字体文件（微软雅黑、黑体等）
        - Linux: 搜索 Noto CJK、WenQuanYi 等开源中文字体
        - macOS: 搜索苹方字体
        - 找不到则返回 None（使用 pygame 默认字体）
        """
        # Windows 常见中文字体
        win_candidates = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"]
        windir = os.environ.get("WINDIR", r"C:\Windows")
        fonts_dir = Path(windir) / "Fonts"
        if fonts_dir.is_dir():
            for name in win_candidates:
                path = fonts_dir / name
                if path.is_file():
                    return str(path)

        # Linux / macOS 常见中文字体路径
        unix_candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
        for path in unix_candidates:
            if Path(path).is_file():
                return path

        return None

    # ─── 渐变缓存 ───

    def _init_gradient_cache(self) -> None:
        for piece_type, colors in PIECE_COLORS.items():
            main, light, dark, _glow_color = colors
            gradient_lines: list[tuple[int, int, int]] = []
            for i in range(CELL_SIZE - 2):
                t = i / (CELL_SIZE - 2)
                if t < 0.3:
                    lt = t / 0.3
                    r = int(light[0] * (1 - lt) + main[0] * lt)
                    g = int(light[1] * (1 - lt) + main[1] * lt)
                    b = int(light[2] * (1 - lt) + main[2] * lt)
                elif t < 0.7:
                    r, g, b = main
                else:
                    lt = (t - 0.7) / 0.3
                    r = int(main[0] * (1 - lt) + dark[0] * lt)
                    g = int(main[1] * (1 - lt) + dark[1] * lt)
                    b = int(main[2] * (1 - lt) + dark[2] * lt)
                gradient_lines.append((r, g, b))
            self._gradient_cache[piece_type] = gradient_lines

    # ─── 游戏逻辑 ───

    def _get_fall_speed(self) -> float:
        return max(0.05, 1.0 - (self.level - 1) * 0.08)

    def _spawn_piece(self) -> None:
        self.piece_generator.refill_queue(5)
        if self.next_piece is None:
            self.current_piece = Piece(self.piece_generator.pop())
            self.next_piece = Piece(self.piece_generator.pop())
        else:
            self.current_piece = self.next_piece
            self.next_piece = Piece(self.piece_generator.pop())
        self.can_hold = True
        self.is_locking = False
        self.lock_timer = 0

        for x, y in self.current_piece.get_cells():
            if y < 0 or x < 0 or x >= COLS or y >= ROWS:
                self.game_over = True
                return
            if self.board[y][x] is not None:
                self.game_over = True
                return

    def _is_valid_position(
        self,
        piece: Piece,
        x_off: int = 0,
        y_off: int = 0,
        rotation: int | None = None,
    ) -> bool:
        if rotation is not None:
            cells = [(piece.x + dx + x_off, piece.y + dy + y_off) for dx, dy in SHAPES[piece.type][rotation]]
        else:
            cells = piece.get_cells_with_offset(x_off, y_off)
        for x, y in cells:
            if x < 0 or x >= COLS or y >= ROWS:
                return False
            if y >= 0 and self.board[y][x] is not None:
                return False
        return True

    def _charge_skill(self, lines: int) -> None:
        self.skill_charges = min(self.skill_max_charges, self.skill_charges + lines)
        if self.skill_charges >= 1:
            self.skill_flash_timer = 0.3

    def _use_skill(self, skill_type: str) -> bool:
        if self.skill_charges <= 0:
            return False
        if self.skill_active:
            return False
        self.skill_charges -= 1
        self.skill_active = True
        self.skill_timer = 0
        self.skill_type = skill_type
        if skill_type == "NUKE":
            self._skill_nuke()
        elif skill_type == "SHOCKWAVE":
            self._skill_shockwave()
        elif skill_type == "LASER":
            self._skill_laser()
        return True

    def _skill_nuke(self) -> None:
        nuke_col = self.current_piece.x + 1 if self.current_piece else COLS // 2
        radius = 2
        self.skill_sweep_rows = []
        for dy in range(-radius, radius + 1):
            row = ROWS // 2 + dy
            if 0 <= row < ROWS:
                cleared = False
                for col in range(
                    max(0, nuke_col - radius),
                    min(COLS, nuke_col + radius + 1),
                ):
                    if self.board[row][col] is not None:
                        self.board[row][col] = None
                        cleared = True
                if cleared:
                    self.skill_sweep_rows.append(row)
        if self.skill_sweep_rows:
            self.skill_sweep_progress = 0
            self._show_notification("核弹!")
            self.screen_flash = ScreenFlashAnimation(
                color=(255, 100, 0),
                duration=0.5,
                intensity=0.5,
            )
        else:
            self.skill_active = False

    def _skill_shockwave(self) -> None:
        shock_row = self.current_piece.y if self.current_piece else ROWS // 2
        self.skill_sweep_rows = [r for r in range(shock_row - 1, shock_row + 2) if 0 <= r < ROWS]
        for row in self.skill_sweep_rows:
            for col in range(COLS):
                if self.board[row][col] is not None:
                    self.board[row][col] = None
        if self.skill_sweep_rows:
            self.skill_sweep_progress = 0
            self._show_notification("冲击波!")
            self.screen_flash = ScreenFlashAnimation(
                color=(0, 200, 255),
                duration=0.4,
                intensity=0.4,
            )
        else:
            self.skill_active = False

    def _skill_laser(self) -> None:
        laser_col = self.current_piece.x + 1 if self.current_piece else COLS // 2
        self.skill_laser_col = max(0, min(COLS - 1, laser_col))
        self.skill_sweep_rows = []
        for row in range(ROWS):
            if self.board[row][self.skill_laser_col] is not None:
                self.board[row][self.skill_laser_col] = None
                self.skill_sweep_rows.append(row)
        if self.skill_sweep_rows:
            self.skill_sweep_progress = 0
            self._show_notification("激光!")
            self.screen_flash = ScreenFlashAnimation(
                color=(255, 0, 100),
                duration=0.4,
                intensity=0.4,
            )
        else:
            self.skill_active = False

    def _add_garbage_rows(self, count: int) -> None:
        for _ in range(count):
            self.garbage_rows.append([None] * COLS)
        if self.garbage_rows:
            self._apply_pending_garbage()

    def _apply_pending_garbage(self) -> None:
        if not self.garbage_rows:
            return
        for _ in range(len(self.garbage_rows)):
            del self.board[0]
            self.board.append(self.garbage_rows.pop(0))
        for row_idx in range(ROWS):
            hole_col = random.randint(0, COLS - 1)
            for col in range(COLS):
                if self.board[row_idx][col] is not None and col != hole_col:
                    self.board[row_idx][col] = "GARBAGE"
        self.pending_garbage = 0

    def _check_tspin(self) -> str:
        p = self.current_piece
        if p is None or p.type != "T":
            return ""
        if not self.last_move_was_rotate:
            return ""
        cx, cy = p.x + 1, p.y + 1
        corners = [
            (cx - 1, cy - 1),
            (cx + 1, cy - 1),
            (cx + 1, cy + 1),
            (cx - 1, cy + 1),
        ]
        filled = 0
        for x, y in corners:
            if y < 0 or (0 <= y < ROWS and 0 <= x < COLS and self.board[y][x] is not None):
                filled += 1
        if filled >= 3:
            return "TSPIN" if self.tspin_trace["was_kick"] else "TSPIN_MINI"
        return ""

    def _show_notification(self, text: str) -> None:
        self.notification_text = text
        self.notification_timer = 1.5

    def _rotate_piece(self, direction: int = 1) -> bool:
        assert self.current_piece is not None
        old_rotation = self.current_piece.rotation
        new_rotation = (old_rotation + direction) % 4
        kick_table = WALL_KICKS["I"] if self.current_piece.type == "I" else WALL_KICKS["default"]
        kicks = kick_table.get((old_rotation, new_rotation), [(0, 0)])

        self.last_move_was_rotate = True
        for dx, dy in kicks:
            if dx != 0 or dy != 0:
                self.tspin_trace["was_kick"] = True
            if self._is_valid_position(self.current_piece, dx, dy, new_rotation):
                self.current_piece.x += dx
                self.current_piece.y += dy
                self.current_piece.rotation = new_rotation
                if self.is_locking:
                    self.lock_timer = 0
                return True

        for dx, dy in [
            (-1, 0),
            (1, 0),
            (-2, 0),
            (2, 0),
            (-1, 1),
            (1, 1),
            (-1, -1),
            (1, -1),
        ]:
            if dx != 0 or dy != 0:
                self.tspin_trace["was_kick"] = True
            if self._is_valid_position(self.current_piece, dx, dy, new_rotation):
                self.current_piece.x += dx
                self.current_piece.y += dy
                self.current_piece.rotation = new_rotation
                if self.is_locking:
                    self.lock_timer = 0
                return True
        self.last_move_was_rotate = False
        return False

    def _move_piece(self, dx: int, dy: int) -> bool:
        assert self.current_piece is not None
        self.last_move_was_rotate = False
        if self._is_valid_position(self.current_piece, dx, dy):
            self.current_piece.x += dx
            self.current_piece.y += dy
            if self.is_locking and dy == 0:
                self.lock_timer = 0
            return True
        return False

    def _hard_drop(self) -> None:
        assert self.current_piece is not None
        drop_distance = 0
        while self._is_valid_position(self.current_piece, 0, 1):
            self.current_piece.y += 1
            drop_distance += 1
        self.score += drop_distance * 2
        self._lock_piece()

    def _get_ghost_y(self) -> int:
        assert self.current_piece is not None
        ghost_y = self.current_piece.y
        while True:
            valid = True
            for dx, dy in SHAPES[self.current_piece.type][self.current_piece.rotation]:
                nx = self.current_piece.x + dx
                ny = ghost_y + dy + 1
                if ny >= ROWS or (ny >= 0 and self.board[ny][nx] is not None):
                    valid = False
                    break
            if not valid:
                break
            ghost_y += 1
        return ghost_y

    def _lock_piece(self) -> None:
        assert self.current_piece is not None
        for x, y in self.current_piece.get_cells():
            if 0 <= y < ROWS and 0 <= x < COLS:
                self.board[y][x] = self.current_piece.type

        full_rows = [r for r in range(ROWS) if all(self.board[r][c] is not None for c in range(COLS))]
        n = len(full_rows)
        spin_type = self._check_tspin()
        is_perfect = n == 4

        anim_type_map: dict[tuple[int, str, bool], str] = {
            (1, "", False): "SINGLE",
            (2, "", False): "DOUBLE",
            (3, "", False): "TRIPLE",
            (4, "", False): "TETRIS",
            (4, "", True): "PERFECT",
            (0, "TSPIN", False): "TSPIN",
            (0, "TSPIN_MINI", False): "TSPIN_MINI",
            (1, "TSPIN", False): "TSPIN",
            (2, "TSPIN", False): "TSPIN",
            (3, "TSPIN", False): "TSPIN",
            (4, "TSPIN", False): "TSPIN",
            (4, "TSPIN", True): "PERFECT",
        }
        anim_key = (n, spin_type, is_perfect and n == 4)
        anim_type = anim_type_map.get(anim_key, "SINGLE")

        if n > 0:
            self.clearing_rows = full_rows
            p = LineClearAnimation.ANIM_PARAMS[anim_type]
            self.clear_animation = create_line_clear_animation(
                full_rows,
                anim_type=anim_type,
                board=self.board,
            )
            self.particles.emit_row(
                full_rows[0],
                self.board_x,
                self.board_y,
                self.board,
            )
            if n > 1:
                self.particles.emit_row(
                    full_rows[-1],
                    self.board_x,
                    self.board_y,
                    self.board,
                )
            self.shake_timer = p["shake_dur"]
            self.shake_intensity = p["shake_i"]
            self.combo_count += 1
            self._charge_skill(n)

            if p["color_burst"]:
                self.color_burst = ColorBurstAnimation(full_rows, self.board)
            if p["screen_flash"]:
                self.screen_flash = ScreenFlashAnimation(
                    color=p["flash_col"],
                    duration=0.2,
                    intensity=0.3,
                )
            if p["extra_glow"]:
                avg_row = sum(full_rows) / len(full_rows)
                center_y = avg_row * CELL_SIZE + CELL_SIZE // 2
                self.shockwave = ShockwaveAnimation(center_y, color=p["flash_col"])
        else:
            self.combo_count = 0
            self._spawn_piece()

        base_score = 0
        if spin_type == "TSPIN":
            line_score = {1: 300, 2: 500, 3: 800, 4: 1000}
            base_score = line_score.get(n, 0)
        elif spin_type == "TSPIN_MINI":
            line_score = {1: 100, 2: 200, 3: 400, 4: 600}
            base_score = line_score.get(n, 0)
        elif n > 0:
            base_score = {1: 100, 2: 300, 3: 500, 4: 800}.get(n, 0)

        b2b_active = is_perfect or (n > 0 and self.b2b_count >= 1)
        if b2b_active and n > 0:
            base_score = int(base_score * 1.5)
            self.b2b_count += 1
        elif n > 0:
            self.b2b_count = 0

        combo_bonus = self.combo_count * 50 if self.combo_count > 1 else 0
        self.score += (base_score + combo_bonus) * self.level
        self.lines_cleared += n
        self.level = self.lines_cleared // 10 + 1
        self.fall_speed = self._get_fall_speed()

        if spin_type == "TSPIN":
            if is_perfect:
                self._show_notification("PERFECT CLEAR!")
            elif n > 0:
                self._show_notification(f"TSPIN {n}L")
        elif spin_type == "TSPIN_MINI":
            self._show_notification("MINI TSPIN")
        elif is_perfect:
            self._show_notification("PERFECT CLEAR!")
        elif n == 4:
            self._show_notification("QUAD!")
        elif self.combo_count >= 3:
            self._show_notification(f"COMBO x{self.combo_count}")

        self.last_move_was_rotate = False
        self.tspin_trace["was_kick"] = False

    def _remove_cleared_rows(self) -> None:
        for row in sorted(self.clearing_rows, reverse=True):
            del self.board[row]
            self.board.insert(0, [None] * COLS)
        self.clearing_rows = []
        self.clear_animation = None
        self.shockwave = None
        self.color_burst = None
        self.screen_flash = None

        full_rows = [r for r in range(ROWS) if all(self.board[r][c] is not None for c in range(COLS))]
        n = len(full_rows)

        if n > 0:
            self.clearing_rows = full_rows
            anim_type_map = {1: "SINGLE", 2: "DOUBLE", 3: "TRIPLE", 4: "TETRIS"}
            anim_type = anim_type_map.get(n, "SINGLE")
            p = LineClearAnimation.ANIM_PARAMS[anim_type]
            self.clear_animation = create_line_clear_animation(
                full_rows,
                anim_type=anim_type,
                board=self.board,
            )
            self.particles.emit_row(
                full_rows[0],
                self.board_x,
                self.board_y,
                self.board,
            )
            if n > 1:
                self.particles.emit_row(
                    full_rows[-1],
                    self.board_x,
                    self.board_y,
                    self.board,
                )
            self.shake_timer = p["shake_dur"]
            self.shake_intensity = p["shake_i"]
            self.combo_count += 1
            self._charge_skill(n)

            if p["color_burst"]:
                self.color_burst = ColorBurstAnimation(full_rows, self.board)
            if p["screen_flash"]:
                self.screen_flash = ScreenFlashAnimation(
                    color=p["flash_col"],
                    duration=0.2,
                    intensity=0.3,
                )
            if p["extra_glow"]:
                avg_row = sum(full_rows) / len(full_rows)
                center_y = avg_row * CELL_SIZE + CELL_SIZE // 2
                self.shockwave = ShockwaveAnimation(center_y, color=p["flash_col"])

            base_score = {1: 100, 2: 300, 3: 500, 4: 800}.get(n, 0)
            combo_bonus = self.combo_count * 50 if self.combo_count > 1 else 0
            self.score += (base_score + combo_bonus) * self.level
            self.lines_cleared += n
            self.level = self.lines_cleared // 10 + 1
            self.fall_speed = self._get_fall_speed()

            if n == 4:
                self._show_notification("QUAD!")
            elif self.combo_count >= 3:
                self._show_notification(f"COMBO x{self.combo_count}")
        else:
            self.skill_charges = 0
            self.skill_active = False
            self.skill_timer = 0
            self.skill_flash_timer = 0
            self.skill_sweep_rows = []
            self.skill_sweep_progress = 0
            self.skill_lightning_rows = []
            self.skill_lightning_progress = 0
            self.pending_garbage = 0
            self.garbage_rows = []
            self.attack_meter = 0
            self.piece_generator.clear()
            self._spawn_piece()

    def _hold_piece(self) -> None:
        assert self.current_piece is not None
        if not self.can_hold:
            return
        self.can_hold = False
        if self.held_piece is None:
            self.held_piece = Piece(self.current_piece.type)
            self._spawn_piece()
        else:
            old_type = self.held_piece.type
            self.held_piece = Piece(self.current_piece.type)
            self.current_piece = Piece(old_type)

    # ─── 渲染 ───

    def _draw_gradient_cell(
        self,
        surface: pygame.Surface,
        x: int,
        y: int,
        colors: tuple[tuple[int, int, int], ...],
        alpha: float = 1.0,
        glow: bool = False,
        piece_type: str | None = None,
    ) -> None:
        main, light, dark, glow_color = colors

        if glow:
            glow_alpha = int(40 * alpha * (0.7 + 0.3 * math.sin(self.glow_timer * 3)))
            glow_surf = pygame.Surface((CELL_SIZE, CELL_SIZE), pygame.SRCALPHA)
            glow_surf.fill((*glow_color, glow_alpha))
            surface.blit(glow_surf, (x, y))

        if piece_type and piece_type in self._gradient_cache:
            gradient_lines = self._gradient_cache[piece_type]
        else:
            gradient_lines = []
            for i in range(CELL_SIZE - 2):
                t = i / (CELL_SIZE - 2)
                if t < 0.3:
                    lt = t / 0.3
                    r = int(light[0] * (1 - lt) + main[0] * lt)
                    g = int(light[1] * (1 - lt) + main[1] * lt)
                    b = int(light[2] * (1 - lt) + main[2] * lt)
                elif t < 0.7:
                    r, g, b = main
                else:
                    lt = (t - 0.7) / 0.3
                    r = int(main[0] * (1 - lt) + dark[0] * lt)
                    g = int(main[1] * (1 - lt) + dark[1] * lt)
                    b = int(main[2] * (1 - lt) + dark[2] * lt)
                gradient_lines.append((r, g, b))

        for i, (r, g, b) in enumerate(gradient_lines):
            if alpha < 1.0:
                ar = min(255, max(0, int(r * alpha)))
                ag = min(255, max(0, int(g * alpha)))
                ab = min(255, max(0, int(b * alpha)))
                color = (ar, ag, ab)
            else:
                color = (r, g, b)
            pygame.draw.line(
                surface,
                color,
                (x + 1, y + 1 + i),
                (x + CELL_SIZE - 2, y + 1 + i),
            )

        # 高光 - 位于方块内部左上区域
        hl_w = CELL_SIZE - 10
        hl_h = (CELL_SIZE - 6) // 3
        hl_x = x + 3
        hl_y = y + 3
        hl_surf = pygame.Surface((hl_w, hl_h), pygame.SRCALPHA)
        hl_surf.fill((255, 255, 255, int(35 * alpha)))
        surface.blit(hl_surf, (hl_x, hl_y))

        # 边框
        border_alpha = int(180 * alpha)
        border_surf = pygame.Surface((CELL_SIZE - 2, CELL_SIZE - 2), pygame.SRCALPHA)
        pygame.draw.rect(
            border_surf,
            (*light, border_alpha),
            (0, 0, CELL_SIZE - 2, CELL_SIZE - 2),
            1,
        )
        surface.blit(border_surf, (x + 1, y + 1))

    def _draw_board(self, surface: pygame.Surface) -> None:
        shake_x, shake_y = 0, 0
        if self.shake_timer > 0:
            shake_x = random.randint(
                -int(self.shake_intensity),
                int(self.shake_intensity),
            )
            shake_y = random.randint(
                -int(self.shake_intensity),
                int(self.shake_intensity),
            )

        bx = self.board_x + shake_x
        by = self.board_y + shake_y

        # 外发光
        glow_alpha = int(30 + 15 * math.sin(self.glow_timer * 2))
        glow_surf = pygame.Surface(
            (BOARD_WIDTH + 16, BOARD_HEIGHT + 16),
            pygame.SRCALPHA,
        )
        glow_surf.fill((*BORDER_GLOW, glow_alpha))
        surface.blit(glow_surf, (bx - 8, by - 8))

        # 棋盘背景
        pygame.draw.rect(
            surface,
            BOARD_BG,
            (bx - 2, by - 2, BOARD_WIDTH + 4, BOARD_HEIGHT + 4),
        )
        pygame.draw.rect(
            surface,
            BORDER_COLOR,
            (bx - 2, by - 2, BOARD_WIDTH + 4, BOARD_HEIGHT + 4),
            2,
        )

        # 网格线
        for row in range(ROWS + 1):
            y = by + row * CELL_SIZE
            pygame.draw.line(surface, GRID_COLOR, (bx, y), (bx + BOARD_WIDTH, y))
        for col in range(COLS + 1):
            x = bx + col * CELL_SIZE
            pygame.draw.line(surface, GRID_COLOR, (x, by), (x, by + BOARD_HEIGHT))

        # 已固定方块
        for row in range(ROWS):
            for col in range(COLS):
                if self.board[row][col] is not None:
                    if self.clear_animation and row in self.clearing_rows:
                        continue
                    piece_type = self.board[row][col]
                    assert piece_type is not None
                    colors = PIECE_COLORS[piece_type]
                    self._draw_gradient_cell(
                        surface,
                        bx + col * CELL_SIZE,
                        by + row * CELL_SIZE,
                        colors,
                        piece_type=piece_type,
                    )

        # 消除动画
        if self.clear_animation:
            self.clear_animation.draw(surface, bx, by)

        # Ghost piece
        if self.current_piece and not self.game_over:
            ghost_y = self._get_ghost_y()
            for dx, dy in SHAPES[self.current_piece.type][self.current_piece.rotation]:
                gx = bx + (self.current_piece.x + dx) * CELL_SIZE
                gy = by + (ghost_y + dy) * CELL_SIZE
                main = self.current_piece.colors[0]
                ghost_surf = pygame.Surface((CELL_SIZE, CELL_SIZE), pygame.SRCALPHA)
                ghost_surf.fill((*main, 40))
                pygame.draw.rect(
                    ghost_surf,
                    (*main, 80),
                    (0, 0, CELL_SIZE, CELL_SIZE),
                    2,
                )
                surface.blit(ghost_surf, (gx, gy))

        # 当前方块
        if self.current_piece and not self.game_over:
            for dx, dy in SHAPES[self.current_piece.type][self.current_piece.rotation]:
                px = bx + (self.current_piece.x + dx) * CELL_SIZE
                py = by + (self.current_piece.y + dy) * CELL_SIZE
                self._draw_gradient_cell(
                    surface,
                    px,
                    py,
                    self.current_piece.colors,
                    glow=True,
                    piece_type=self.current_piece.type,
                )

        # 粒子和特效
        self.particles.draw(surface)
        if self.shockwave:
            self.shockwave.draw(surface, bx, by)
        if self.color_burst:
            self.color_burst.draw(surface, bx, by)

    def _draw_mini_piece(
        self,
        surface: pygame.Surface,
        piece_type: str | None,
        x: int,
        y: int,
        cell_size: int = 20,
    ) -> None:
        if piece_type is None:
            return
        colors = PIECE_COLORS[piece_type]
        shape = SHAPES[piece_type][0]
        min_x = min(dx for dx, dy in shape)
        max_x = max(dx for dx, dy in shape)
        min_y = min(dy for dx, dy in shape)
        max_y = max(dy for dx, dy in shape)
        w = (max_x - min_x + 1) * cell_size
        h = (max_y - min_y + 1) * cell_size
        ox = x - w // 2
        oy = y - h // 2

        for dx, dy in shape:
            cx = ox + (dx - min_x) * cell_size
            cy = oy + (dy - min_y) * cell_size
            main, light, dark, _ = colors

            for i in range(cell_size):
                t = i / cell_size
                if t < 0.3:
                    lt = t / 0.3
                    r = int(light[0] * (1 - lt) + main[0] * lt)
                    g = int(light[1] * (1 - lt) + main[1] * lt)
                    b = int(light[2] * (1 - lt) + main[2] * lt)
                else:
                    lt = (t - 0.3) / 0.7
                    r = int(main[0] * (1 - lt) + dark[0] * lt)
                    g = int(main[1] * (1 - lt) + dark[1] * lt)
                    b = int(main[2] * (1 - lt) + dark[2] * lt)
                pygame.draw.line(
                    surface,
                    (r, g, b),
                    (cx, cy + i),
                    (cx + cell_size - 1, cy + i),
                )
            pygame.draw.rect(surface, light, (cx, cy, cell_size, cell_size), 1)

    def _draw_text(
        self,
        surface: pygame.Surface,
        text: str,
        x: int,
        y: int,
        color: tuple[int, int, int],
        font: pygame.font.Font,
        anchor_x: str = "left",
        anchor_y: str = "top",
    ) -> None:
        rendered = font.render(text, True, color)
        rect = rendered.get_rect()
        if anchor_x == "center":
            rect.centerx = x
        elif anchor_x == "right":
            rect.right = x
        else:
            rect.left = x
        if anchor_y == "center":
            rect.centery = y
        elif anchor_y == "bottom":
            rect.bottom = y
        else:
            rect.top = y
        surface.blit(rendered, rect)

    def _draw_control_key(
        self,
        surface: pygame.Surface,
        x: int,
        y: int,
        key_text: str,
        action_text: str,
        key_color: tuple[int, int, int],
    ) -> None:
        key_width = 45 if len(key_text) > 1 else 28
        key_height = 20
        pygame.draw.rect(surface, key_color, (x, y, key_width, key_height))
        self._draw_text(
            surface,
            key_text,
            x + key_width // 2,
            y + key_height // 2,
            (255, 255, 255),
            self._font_key,
            anchor_x="center",
            anchor_y="center",
        )
        self._draw_text(
            surface,
            action_text,
            x + key_width + 8,
            y + 2,
            (140, 140, 180),
            self._font_small,
        )

    def _draw_sidebar(self, surface: pygame.Surface) -> None:
        sx = self.board_x + BOARD_WIDTH + 20
        sy = self.board_y

        # 侧栏背景
        pygame.draw.rect(
            surface,
            SIDEBAR_BG,
            (sx - 5, sy - 5, SIDEBAR_WIDTH, BOARD_HEIGHT + 10),
        )
        pygame.draw.rect(
            surface,
            BORDER_COLOR,
            (sx - 5, sy - 5, SIDEBAR_WIDTH, BOARD_HEIGHT + 10),
            1,
        )

        # 标题
        self._draw_text(
            surface,
            "GK TETRIS",
            sx + 10,
            sy + 10,
            ACCENT_COLOR,
            self._font_large,
        )

        # 分数
        y_pos = sy + 60
        self._draw_text(
            surface,
            "分数",
            sx + 10,
            y_pos,
            (140, 140, 180),
            self._font_small,
        )
        self._draw_text(
            surface,
            f"{self.score:,}",
            sx + 10,
            y_pos + 22,
            ACCENT_COLOR,
            self._font_medium,
        )

        # 等级
        y_pos += 50
        self._draw_text(
            surface,
            "等级",
            sx + 10,
            y_pos,
            (140, 140, 180),
            self._font_small,
        )
        self._draw_text(
            surface,
            str(self.level),
            sx + 10,
            y_pos + 22,
            ACCENT_COLOR,
            self._font_medium,
        )

        # 消除
        y_pos += 50
        self._draw_text(
            surface,
            "消除",
            sx + 10,
            y_pos,
            (140, 140, 180),
            self._font_small,
        )
        self._draw_text(
            surface,
            str(self.lines_cleared),
            sx + 10,
            y_pos + 22,
            ACCENT_COLOR,
            self._font_medium,
        )

        # 下一个
        y_pos += 55
        self._draw_text(
            surface,
            "下一个",
            sx + 10,
            y_pos,
            (140, 140, 180),
            self._font_small,
        )
        preview_x = sx + 10
        preview_y = y_pos + 20
        preview_w = SIDEBAR_WIDTH - 30
        preview_h = 60
        pygame.draw.rect(
            surface,
            (15, 15, 30),
            (preview_x, preview_y, preview_w, preview_h),
        )
        pygame.draw.rect(
            surface,
            BORDER_COLOR,
            (preview_x, preview_y, preview_w, preview_h),
            1,
        )
        if self.next_piece:
            self._draw_mini_piece(
                surface,
                self.next_piece.type,
                sx + 10 + (SIDEBAR_WIDTH - 30) // 2,
                y_pos + 50,
                cell_size=22,
            )

        # 队列
        y_pos += 85
        self._draw_text(
            surface,
            "队列",
            sx + 10,
            y_pos,
            (140, 140, 180),
            self._font_small,
        )
        queue_x = sx + 10
        queue_y = y_pos + 20
        queue_w = SIDEBAR_WIDTH - 30
        queue_h = 50
        pygame.draw.rect(surface, (15, 15, 30), (queue_x, queue_y, queue_w, queue_h))
        pygame.draw.rect(surface, BORDER_COLOR, (queue_x, queue_y, queue_w, queue_h), 1)
        for i in range(3):
            ty = self.piece_generator.peek(i) if hasattr(self, "piece_generator") else "I"
            self._draw_mini_piece(
                surface,
                ty,
                sx + 10 + 30 + i * 50,
                y_pos + 45,
                cell_size=14,
            )

        # 操作说明
        y_pos = sy + BOARD_HEIGHT - 240
        self._draw_text(
            surface,
            "操作说明",
            sx + 10,
            y_pos,
            ACCENT_COLOR,
            self._font_small_bold,
        )

        # 背景框
        ctrl_y = y_pos + 25
        ctrl_box_h = 210
        ctrl_x = sx + 10
        ctrl_w = SIDEBAR_WIDTH - 30
        pygame.draw.rect(surface, (15, 15, 30), (ctrl_x, ctrl_y, ctrl_w, ctrl_box_h))
        pygame.draw.rect(surface, BORDER_COLOR, (ctrl_x, ctrl_y, ctrl_w, ctrl_box_h), 1)

        y_start = ctrl_y + 10
        self._draw_control_key(
            surface,
            sx + 18,
            y_start,
            "<->",
            "移动",
            (100, 140, 255),
        )
        self._draw_control_key(
            surface,
            sx + 18,
            y_start + 35,
            "^",
            "旋转",
            (180, 60, 255),
        )
        self._draw_control_key(
            surface,
            sx + 18,
            y_start + 70,
            "v",
            "软降",
            (50, 255, 100),
        )
        self._draw_control_key(
            surface,
            sx + 18,
            y_start + 105,
            "空格",
            "硬降",
            (255, 150, 30),
        )

        y_other = y_start + 145
        self._draw_text(
            surface,
            "P 暂停",
            sx + 18,
            y_other,
            (140, 140, 180),
            self._font_small,
        )
        self._draw_text(
            surface,
            "ESC 退出",
            sx + 18,
            y_other + 28,
            (140, 140, 180),
            self._font_small,
        )

    def _draw_background(self, surface: pygame.Surface) -> None:
        surface.fill(BG_COLOR)
        glow_val = math.sin(self.glow_timer * 2)
        for sx, sy, size, brightness in self.stars:
            alpha = brightness * (0.5 + 0.5 * glow_val)
            color_val = int(80 * alpha)
            pygame.draw.circle(
                surface,
                (color_val, color_val, color_val + 20),
                (int(sx), int(sy)),
                max(1, int(size)),
            )

    def _draw_game_over(self, surface: pygame.Surface) -> None:
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 150))
        surface.blit(overlay, (0, 0))

        cx = SCREEN_WIDTH // 2
        cy = SCREEN_HEIGHT // 2
        self._draw_text(
            surface,
            "游戏结束",
            cx,
            cy - 20,
            (255, 80, 80),
            self._font_large,
            anchor_x="center",
            anchor_y="center",
        )
        self._draw_text(
            surface,
            f"最终分数: {self.score:,}",
            cx,
            cy + 20,
            TEXT_COLOR,
            self._font_medium_plain,
            anchor_x="center",
            anchor_y="center",
        )
        if int(self.glow_timer * 2) % 2:
            self._draw_text(
                surface,
                "按 R 重新开始",
                cx,
                cy + 60,
                ACCENT_COLOR,
                self._font_medium_plain,
                anchor_x="center",
                anchor_y="center",
            )

    def _draw_paused(self, surface: pygame.Surface) -> None:
        overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 120))
        surface.blit(overlay, (0, 0))

        cx = SCREEN_WIDTH // 2
        cy = SCREEN_HEIGHT // 2
        self._draw_text(
            surface,
            "暂停",
            cx,
            cy - 20,
            TEXT_COLOR,
            self._font_large,
            anchor_x="center",
            anchor_y="center",
        )
        self._draw_text(
            surface,
            "按 P 继续",
            cx,
            cy + 25,
            ACCENT_COLOR,
            self._font_medium_plain,
            anchor_x="center",
            anchor_y="center",
        )

    def _draw_notification(self, surface: pygame.Surface) -> None:
        if self.notification_timer <= 0 or not self.notification_text:
            return
        fade_start = 1.2
        if self.notification_timer < fade_start:
            alpha = self.notification_timer / fade_start
        else:
            alpha = min(1.0, self.notification_timer / 0.3)

        if self.notification_text.startswith("TSPIN"):
            notif_color = (200, 80, 255)
        elif self.notification_text.startswith("MINI"):
            notif_color = (180, 120, 255)
        elif self.notification_text in ("QUAD", "PERFECT CLEAR!"):
            notif_color = (255, 220, 50)
        elif self.notification_text.startswith("COMBO"):
            notif_color = (255, 160, 50)
        else:
            notif_color = (100, 200, 255)

        notif_surf = pygame.Surface((BOARD_WIDTH, 60), pygame.SRCALPHA)
        text_rendered = self._font_medium_plain.render(
            self.notification_text,
            True,
            notif_color,
        )
        text_rendered.set_alpha(int(255 * alpha))
        rect = text_rendered.get_rect(center=(BOARD_WIDTH // 2, 30))
        notif_surf.blit(text_rendered, rect)
        nx = self.board_x
        ny = self.board_y + BOARD_HEIGHT // 3
        surface.blit(notif_surf, (nx, ny))

    def _draw_fps(self, surface: pygame.Surface) -> None:
        if self.fps >= 200:
            fps_color = (80, 255, 80)
        elif self.fps >= 120:
            fps_color = (100, 255, 100)
        elif self.fps >= 60:
            fps_color = (200, 255, 0)
        elif self.fps >= 30:
            fps_color = (255, 200, 0)
        else:
            fps_color = (255, 80, 80)
        self._draw_text(surface, f"FPS: {self.fps}", 10, 10, fps_color, self._font_fps)

    # ─── 重置 ───

    def reset(self) -> None:
        self.board = [[None] * COLS for _ in range(ROWS)]
        self.current_piece = None
        self.next_piece = None
        self.held_piece = None
        self.can_hold = True
        self.score = 0
        self.level = 1
        self.lines_cleared = 0
        self.game_over = False
        self.paused = False
        self.down_pressed = False
        self.fall_timer = 0
        self.fall_speed = self._get_fall_speed()
        self.lock_timer = 0
        self.is_locking = False
        self.particles = ParticleSystem()
        self.clear_animation = None
        self.clearing_rows = []
        self.shake_timer = 0
        self.combo_count = 0
        self.b2b_count = 0
        self.last_move_was_rotate = False
        self.tspin_trace["was_kick"] = False
        self.notification_text = ""
        self.notification_timer = 0
        self.shockwave = None
        self.color_burst = None
        self.screen_flash = None
        self.fps = 0
        self.frame_count = 0
        self.fps_timer = 0
        self._spawn_piece()

    # ─── 事件处理 ───

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                return

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                    return

                if self.game_over:
                    if event.key == pygame.K_r:
                        self.reset()
                    continue

                if event.key == pygame.K_p:
                    self.paused = not self.paused
                    continue

                if self.paused:
                    continue

                if self.clear_animation:
                    continue

                if event.key == pygame.K_LEFT:
                    self._move_piece(-1, 0)
                    self.das_direction = -1
                    self.das_timer = 0
                    self.das_active = False
                elif event.key == pygame.K_RIGHT:
                    self._move_piece(1, 0)
                    self.das_direction = 1
                    self.das_timer = 0
                    self.das_active = False
                elif event.key == pygame.K_UP:
                    self._rotate_piece(1)
                elif event.key == pygame.K_z:
                    self._rotate_piece(-1)
                elif event.key == pygame.K_DOWN:
                    self._move_piece(0, 1)
                    self.fall_timer = 0
                    self.down_pressed = True
                elif event.key == pygame.K_SPACE:
                    self._hard_drop()
                elif event.key == pygame.K_c:
                    self._hold_piece()
                elif event.key == pygame.K_x:
                    self._use_skill("NUKE")
                elif event.key == pygame.K_s:
                    self._use_skill("SHOCKWAVE")
                elif event.key == pygame.K_f:
                    self._use_skill("LASER")

            if event.type == pygame.KEYUP:
                if event.key in (pygame.K_LEFT, pygame.K_RIGHT):
                    self.das_direction = 0
                    self.das_active = False
                if event.key == pygame.K_DOWN:
                    self.down_pressed = False

    # ─── 更新 ───

    def on_update(self, delta_time: float) -> None:
        self.glow_timer += delta_time

        # FPS 计算
        self.frame_count += 1
        self.fps_timer += delta_time
        if self.fps_timer >= self.fps_display_interval:
            self.fps = int(self.frame_count / self.fps_timer)
            self.frame_count = 0
            self.fps_timer = 0

        if self.paused or self.game_over:
            return

        if self.das_direction != 0 and not self.clear_animation:
            self.das_timer += delta_time
            if not self.das_active and self.das_timer >= self.das_delay:
                self.das_active = True
                self.das_timer = 0
            if self.das_active and self.das_timer >= self.das_repeat:
                self._move_piece(self.das_direction, 0)
                self.das_timer = 0

        if self.clear_animation:
            self.clear_animation.update(delta_time)
            self.particles.update(delta_time)
            if not self.clear_animation.active:
                self._remove_cleared_rows()
        else:
            self.fall_timer += delta_time
            if self.fall_timer >= self.fall_speed:
                self.fall_timer = 0
                if self._move_piece(0, 1):
                    if self.down_pressed:
                        self.score += 1
                elif not self.is_locking:
                    self.is_locking = True
                    self.lock_timer = 0

            if self.is_locking:
                self.lock_timer += delta_time
                assert self.current_piece is not None
                if self._is_valid_position(self.current_piece, 0, 1):
                    self.is_locking = False
                    self.lock_timer = 0
                elif self.lock_timer >= self.lock_delay:
                    self._lock_piece()

        if self.shake_timer > 0:
            self.shake_timer -= delta_time
            self.shake_intensity *= 0.9

        self.particles.update(delta_time)
        if self.shockwave:
            self.shockwave.update(delta_time)
            if not self.shockwave.active:
                self.shockwave = None
        if self.color_burst:
            self.color_burst.update(delta_time)
            if not self.color_burst.active:
                self.color_burst = None
        if self.screen_flash:
            self.screen_flash.update(delta_time)
            if not self.screen_flash.active:
                self.screen_flash = None

        if self.notification_timer > 0:
            self.notification_timer -= delta_time

    # ─── 渲染 ───

    def _render(self) -> None:
        self._draw_background(self.screen)
        self._draw_board(self.screen)
        self._draw_notification(self.screen)
        self._draw_sidebar(self.screen)
        self._draw_fps(self.screen)

        if self.screen_flash:
            self.screen_flash.draw(self.screen)

        if self.game_over:
            self._draw_game_over(self.screen)
        if self.paused:
            self._draw_paused(self.screen)

    # ─── 主循环 ───

    def run(self) -> None:
        while self.running:
            dt = self.clock.tick(FPS) / 1000.0
            self._handle_events()
            self.on_update(dt)
            self._render()
            pygame.display.flip()
        pygame.quit()
        sys.exit()


def main() -> None:
    game = TetrisGame()
    game.run()


if __name__ == "__main__":
    main()
