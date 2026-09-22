"""Durable processing history UI."""

from __future__ import annotations

import json
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from mailarchive.workspace import WorkspaceStore

PAGE_SIZE = 100


class ProcessingHistoryDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        state: WorkspaceStore,
        open_directory: Callable[[Path], None],
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.open_directory = open_directory
        self.items: list[dict[str, object]] = []
        self.title("Processing history")
        self.geometry("1180x700")
        self.transient(parent)

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Message intake and accepted archive plans",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        columns = ("time", "type", "source", "received", "rule", "status", "subject")
        self.history = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        headings = {
            "time": "Processed",
            "type": "Stage",
            "source": "Source",
            "received": "Received",
            "rule": "Frozen rule",
            "status": "Status",
            "subject": "Subject",
        }
        widths = {
            "time": 155,
            "type": 75,
            "source": 180,
            "received": 155,
            "rule": 140,
            "status": 90,
            "subject": 270,
        }
        for column in columns:
            self.history.heading(column, text=headings[column])
            self.history.column(column, width=widths[column], minwidth=70)
        self.history.pack(fill="both", expand=True)
        self.history.bind("<<TreeviewSelect>>", self.show_selected)

        ttk.Label(frame, text="Details", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", pady=(12, 4)
        )
        self.detail = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.detail.pack(fill="x")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left")
        self.more_button = ttk.Button(buttons, text="Load more", command=self.load_more)
        self.more_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Open selected output folder", command=self.open_selected).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.refresh()

    def refresh(self) -> None:
        self.items = []
        children = self.history.get_children()
        if children:
            self.history.delete(*children)
        self._before: tuple[str, str] | None = None
        self.load_more()
        self._set_detail("Select a history entry to see its durable details.")

    def load_more(self) -> None:
        page = self.state.processing_history(PAGE_SIZE + 1, before=self._before)
        visible = page[:PAGE_SIZE]
        start = len(self.items)
        self.items.extend(visible)
        for index, item in enumerate(visible, start=start):
            self.history.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    item["occurred_at"],
                    item["item_type"],
                    item["address"],
                    item["received_at"] or "",
                    item["rule_name"] or "",
                    item["status"],
                    item["subject"] or "",
                ),
            )
        if visible:
            last = visible[-1]
            self._before = (str(last["occurred_at"]), str(last["history_key"]))
        self.more_button.configure(state="normal" if len(page) > PAGE_SIZE else "disabled")

    def _selected(self) -> dict[str, object] | None:
        selection = self.history.selection()
        return self.items[int(selection[0])] if selection else None

    def _set_detail(self, value: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", value)
        self.detail.configure(state="disabled")

    def show_selected(self, _event=None) -> None:
        item = self._selected()
        if item is None:
            return
        lines = [
            f"Source: {item['address']} ({item['provider']})",
            f"Run: {item['run_kind']}",
            f"Received: {item['received_at'] or 'not available'}"
            + (f" ({item['received_origin']})" if item.get("received_origin") else ""),
            f"Status: {item['status']}",
        ]
        if item["error"]:
            lines.append(f"Error: {item['error']}")
        if item["item_type"] == "plan":
            snapshot = json.loads(str(item["rule_json"]))
            lines.append(f"Frozen rule: {snapshot['rule']['name']}")
            for target in self.state.plan_targets(str(item["id"])):
                detail = f"Destination {target['status']}: {target['path']}"
                if target["error"]:
                    detail += f" ({target['error']})"
                lines.append(detail)
                for output in self.state.target_outputs(str(item["id"]), target["target_id"]):
                    output_detail = f"  Output {output['status']}: {output['final_path']}"
                    if output["error"]:
                        output_detail += f" ({output['error']})"
                    lines.append(output_detail)
        self._set_detail("\n".join(lines))

    def open_selected(self) -> None:
        item = self._selected()
        if item is None or item["item_type"] != "plan":
            messagebox.showinfo(
                "No archive output",
                "Select an accepted archive plan with a completed output.",
                parent=self,
            )
            return
        folders = list(
            dict.fromkeys(
                str(Path(output["final_path"]).parent)
                for target in self.state.plan_targets(str(item["id"]))
                for output in self.state.target_outputs(str(item["id"]), str(target["target_id"]))
                if output["status"] == "done"
            )
        )
        if not folders:
            messagebox.showinfo(
                "No completed output",
                "This plan has no completed output folder yet.",
                parent=self,
            )
            return
        if len(folders) == 1:
            chosen = folders[0]
        else:
            options = "\n".join(f"{index}. {path}" for index, path in enumerate(folders, 1))
            index = simpledialog.askinteger(
                "Open output folder",
                options,
                minvalue=1,
                maxvalue=len(folders),
                parent=self,
            )
            if index is None:
                return
            chosen = folders[index - 1]
        try:
            self.open_directory(Path(chosen))
        except Exception as exc:
            messagebox.showerror("Could not open folder", str(exc), parent=self)
