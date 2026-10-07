"""Keep every action visible when larger fonts require additional rows."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from tkinter import ttk


class ResponsiveActions(ttk.Frame):
    def __init__(self, parent, actions: Sequence[tuple[str, Callable[[], None]]]) -> None:
        super().__init__(parent)
        self.buttons = [ttk.Button(self, text=text, command=command) for text, command in actions]
        self._positions: tuple[tuple[int, int], ...] = ()
        self.bind("<Configure>", lambda event: self._layout(event.width))
        self._layout(1)

    def _layout(self, width: int) -> None:
        positions = []
        row = column = used = 0
        for button in self.buttons:
            requested = button.winfo_reqwidth() + 8
            if column and used + requested > width:
                row += 1
                column = used = 0
            positions.append((row, column))
            column += 1
            used += requested
        if tuple(positions) == self._positions:
            return
        self._positions = tuple(positions)
        for button, (row, column) in zip(self.buttons, positions, strict=True):
            button.grid(row=row, column=column, sticky="w", padx=(0, 8), pady=(0, 4))
