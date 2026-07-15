"""Piece module for entities."""

from __future__ import annotations

import random

from ..conf import COLS, PIECE_COLORS, SHAPES


class Piece:
    def __init__(self, piece_type=None) -> None:
        if piece_type is None:
            piece_type = random.choice(list(SHAPES.keys()))
        self.type = piece_type
        self.rotation = 0
        self.x = COLS // 2 - 2
        self.y = 0
        self.colors = PIECE_COLORS[piece_type]

    def get_cells(self):
        return [(self.x + dx, self.y + dy) for dx, dy in SHAPES[self.type][self.rotation]]

    def get_cells_with_offset(self, x_off, y_off):
        return [(self.x + dx + x_off, self.y + dy + y_off) for dx, dy in SHAPES[self.type][self.rotation]]


class PieceGenerator:
    def __init__(self) -> None:
        self.bag = []
        self.queue = []

    def fill_bag(self) -> None:
        self.bag = list(SHAPES.keys())
        random.shuffle(self.bag)

    def next(self) -> str:
        if not self.bag:
            self.fill_bag()
        if not self.bag:
            return random.choice(list(SHAPES.keys()))
        return self.bag.pop()

    def refill_queue(self, target_size=5) -> None:
        while len(self.queue) < target_size:
            self.queue.append(self.next())

    def pop(self) -> str:
        if not self.queue:
            self.refill_queue()
        return self.queue.pop(0) if self.queue else self.next()

    def peek(self, index=0) -> str:
        while len(self.queue) <= index:
            self.queue.append(self.next())
        return self.queue[index]

    def clear(self) -> None:
        self.bag = []
        self.queue = []