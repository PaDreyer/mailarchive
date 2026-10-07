"""Bounded vertical content with mouse and keyboard focus scrolling."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from weakref import WeakSet


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, width: int = 1, height: int = 1) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(
            self, width=width, height=height, highlightthickness=0, takefocus=False
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas)
        self.content_window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_content)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._bound_widgets: WeakSet[tk.Misc] = WeakSet()

    def _resize_content(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.content_window, width=event.width)

    def _update_scrollregion(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def bind_widgets(self, widget: tk.Misc | None = None) -> None:
        widget = self if widget is None else widget
        self._bind_widget(widget)
        for child in widget.winfo_children():
            self.bind_widgets(child)

    def _bind_widget(self, widget: tk.Misc) -> None:
        if widget not in self._bound_widgets:
            widget.bind("<MouseWheel>", self._scroll, add="+")
            widget.bind("<Button-4>", self._scroll, add="+")
            widget.bind("<Button-5>", self._scroll, add="+")
            widget.bind("<FocusIn>", lambda event: self.see(event.widget), add="+")
            self._bound_widgets.add(widget)
            parent = self.master
            while parent is not None:
                if isinstance(parent, ScrollableFrame):
                    parent._bind_widget(widget)
                parent = parent.master

    def _scroll(self, event: tk.Event) -> str | None:
        if event.num in (4, 5):
            units = -1 if event.num == 4 else 1
        else:
            units = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        before = self.canvas.yview()
        self.canvas.yview_scroll(units, "units")
        return "break" if self.canvas.yview() != before else None

    def see(self, widget: tk.Misc) -> None:
        self.update_idletasks()
        if not str(widget).startswith(f"{self.content}."):
            return
        parent = widget.master
        while parent is not None and parent is not self.canvas:
            if isinstance(parent, ScrollableFrame):
                # Reveal the input inside the nested frame first. Its viewport
                # can be taller than ours, so align the input, not that viewport.
                parent.see(widget)
                break
            parent = parent.master
        top = widget.winfo_rooty() - self.content.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        height = self.canvas.winfo_height()
        if top < visible_top:
            visible_top = top
        elif bottom > visible_top + height:
            visible_top = bottom - height
        else:
            return
        self.canvas.yview_moveto(visible_top / max(1, self.content.winfo_height()))
