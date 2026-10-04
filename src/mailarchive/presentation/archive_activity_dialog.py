"""Archive activity view backed by typed application read models."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk

from mailarchive.application.activity import ActivityDetail, ActivityItem, OutputResult
from mailarchive.application.session import MailArchiveApplication

PAGE_SIZE = 100


def _status_label(status: str) -> str:
    return {
        "running": "Running",
        "waiting": "Waiting to retry",
        "complete": "Completed",
        "done": "Completed",
        "working": "Processing",
        "pending": "Pending",
        "queued": "Queued",
        "failed": "Failed",
        "error": "Error",
        "stopped": "Stopped",
        "aborted": "Stopped",
        "no_output": "No output",
    }.get(status, status.replace("_", " ").capitalize())


class ArchiveActivityDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        application: MailArchiveApplication,
        open_output_path: Callable[[Path], None],
    ) -> None:
        super().__init__(parent)
        self.application = application
        self.open_output_path = open_output_path
        self.title("Archive activity")
        self.geometry("1220x760")
        self.transient(parent)
        self.current_items: tuple[ActivityItem, ...] = ()
        self.history_items: list[ActivityItem] = []
        self._next_before: tuple[str, str] | None = None
        self._selected_output: OutputResult | None = None
        self._detail_key: str | None = None
        self._refresh_timer: str | None = None

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Archive activity", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Current jobs and past mail results, including saved files and errors.",
        ).pack(anchor="w", pady=(3, 10))

        lists = ttk.Panedwindow(frame, orient="horizontal")
        lists.pack(fill="both", expand=True)
        current_frame = ttk.LabelFrame(lists, text="Current jobs", padding=8)
        history_frame = ttk.LabelFrame(lists, text="History", padding=8)
        lists.add(current_frame, weight=1)
        lists.add(history_frame, weight=1)
        columns = ("time", "kind", "status", "summary")
        self.current_tree = self._make_list(current_frame, columns)
        self.history_tree = self._make_list(history_frame, columns)
        self.current_tree.bind("<<TreeviewSelect>>", self._select_current)
        self.history_tree.bind("<<TreeviewSelect>>", self._select_history)
        self.empty_current = tk.StringVar(value="")
        ttk.Label(current_frame, textvariable=self.empty_current).pack(anchor="w", pady=(4, 0))
        history_controls = ttk.Frame(history_frame)
        history_controls.pack(fill="x", pady=(4, 0))
        self.history_summary = tk.StringVar(value="")
        ttk.Label(history_controls, textvariable=self.history_summary).pack(side="left")
        self.more_button = ttk.Button(history_controls, text="Load more", command=self.load_more)
        self.more_button.pack(side="right")

        detail_frame = ttk.LabelFrame(frame, text="Selected job", padding=8)
        detail_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.detail_summary = tk.StringVar(value="Select a job to see its mail and outputs.")
        ttk.Label(detail_frame, textvariable=self.detail_summary, wraplength=1140).pack(
            anchor="w", pady=(0, 6)
        )
        result_table = ttk.Frame(detail_frame)
        result_table.pack(fill="both", expand=True)
        self.result_tree = ttk.Treeview(
            result_table, columns=("status", "path", "error"), show="tree headings", height=8
        )
        self.result_tree.heading("#0", text="Item")
        self.result_tree.heading("status", text="Status")
        self.result_tree.heading("path", text="Output path")
        self.result_tree.heading("error", text="Error")
        self.result_tree.column("#0", width=280)
        self.result_tree.column("status", width=100)
        self.result_tree.column("path", width=440)
        self.result_tree.column("error", width=290)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        result_vertical = ttk.Scrollbar(
            result_table, orient="vertical", command=self.result_tree.yview
        )
        result_horizontal = ttk.Scrollbar(
            result_table, orient="horizontal", command=self.result_tree.xview
        )
        result_vertical.grid(row=0, column=1, sticky="ns")
        result_horizontal.grid(row=1, column=0, sticky="ew")
        self.result_tree.configure(
            yscrollcommand=result_vertical.set, xscrollcommand=result_horizontal.set
        )
        result_table.columnconfigure(0, weight=1)
        result_table.rowconfigure(0, weight=1)
        self.result_tree.bind("<<TreeviewSelect>>", self._select_result)
        self._output_by_row: dict[str, OutputResult] = {}

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="Refresh", command=self.refresh).pack(side="left")
        self.stop_button = ttk.Button(
            controls, text="Stop selected job", command=self.stop_selected
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(
            controls, text="Retry selected failure", command=self.retry_selected
        )
        self.retry_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(
            controls, text="Open selected output", command=self.open_selected
        )
        self.open_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Close", command=self.destroy).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.refresh()
        self._schedule_refresh()

    @staticmethod
    def _make_list(parent: ttk.Frame, columns: tuple[str, ...]) -> ttk.Treeview:
        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True)
        tree = ttk.Treeview(table, columns=columns, show="headings", height=9)
        for name, label, width in (
            ("time", "Started", 150),
            ("kind", "Type", 80),
            ("status", "Status", 95),
            ("summary", "Summary", 290),
        ):
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=65)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=tree.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        return tree

    @staticmethod
    def _fill(tree: ttk.Treeview, items: tuple[ActivityItem, ...] | list[ActivityItem]) -> None:
        selected = tree.selection()
        selected_key = selected[0] if selected else None
        children = tree.get_children()
        if children:
            tree.delete(*children)
        for item in items:
            tree.insert(
                "",
                "end",
                iid=item.key,
                values=(
                    item.occurred_at,
                    item.kind.title(),
                    _status_label(item.status),
                    item.summary,
                ),
            )
        if selected_key and tree.exists(selected_key):
            tree.selection_set(selected_key)

    def refresh(self) -> None:
        try:
            self.current_items = self.application.current_jobs()
            page = self.application.activity_page(limit=PAGE_SIZE)
        except Exception as exc:
            messagebox.showerror("Could not load archive activity", str(exc), parent=self)
            return
        self.history_items = list(page.items)
        self._next_before = page.next_before
        self._fill(self.current_tree, self.current_items)
        self._fill(self.history_tree, self.history_items)
        self.empty_current.set("No archive jobs are running." if not self.current_items else "")
        self._update_history_controls()
        self._update_selection()

    def load_more(self) -> None:
        if self._next_before is None:
            return
        try:
            page = self.application.activity_page(before=self._next_before, limit=PAGE_SIZE)
        except Exception as exc:
            messagebox.showerror("Could not load history", str(exc), parent=self)
            return
        self.history_items.extend(page.items)
        self._next_before = page.next_before
        self._fill(self.history_tree, self.history_items)
        self._update_history_controls()

    def _update_history_controls(self) -> None:
        count = len(self.history_items)
        self.history_summary.set(f"{count} history entries" if count else "No archive history yet.")
        self.more_button.configure(state="normal" if self._next_before else "disabled")

    def _select_current(self, _event=None) -> None:
        if self.current_tree.selection():
            other = self.history_tree.selection()
            if other:
                self.history_tree.selection_remove(*other)
        self._update_selection()

    def _select_history(self, _event=None) -> None:
        if self.history_tree.selection():
            other = self.current_tree.selection()
            if other:
                self.current_tree.selection_remove(*other)
        self._update_selection()

    def _selected_item(self) -> ActivityItem | None:
        current = self.current_tree.selection()
        if current:
            return next((item for item in self.current_items if item.key == current[0]), None)
        history = self.history_tree.selection()
        return (
            next((item for item in self.history_items if item.key == history[0]), None)
            if history
            else None
        )

    def _update_selection(self) -> None:
        item = self._selected_item()
        same_job = item is not None and self._detail_key == item.key
        first, last = self.result_tree.yview()
        first_row = round(first * self._result_row_count()) if same_job else 0
        previous_output_id = (
            self._selected_output.output_id if same_job and self._selected_output else None
        )
        self.stop_button.configure(state="normal" if item and item.can_stop else "disabled")
        self.retry_button.configure(state="normal" if item and item.can_retry else "disabled")
        self._selected_output = None
        self.open_button.configure(state="disabled")
        self._output_by_row.clear()
        self.result_tree.delete(*self.result_tree.get_children())
        if item is None:
            self._detail_key = None
            self.detail_summary.set("Select a job to see its mail and outputs.")
            return
        try:
            detail = self.application.activity_detail(item.key)
        except Exception as exc:
            self.detail_summary.set(f"Could not load job details: {exc}")
            return
        self._detail_key = item.key
        self._show_detail(detail)
        if previous_output_id is not None:
            for row, output in self._output_by_row.items():
                if output.output_id == previous_output_id:
                    self.result_tree.selection_set(row)
                    self._select_result()
                    break
        if same_job and last >= 1.0:
            self.result_tree.yview_moveto(1.0)
        else:
            self.result_tree.yview_moveto(first_row / max(1, self._result_row_count()))

    def _result_row_count(self, parent: str = "") -> int:
        return sum(
            1 + (self._result_row_count(row) if self.result_tree.item(row, "open") else 0)
            for row in self.result_tree.get_children(parent)
        )

    def _show_detail(self, detail: ActivityDetail) -> None:
        item = detail.item
        summary = (
            f"{item.summary} — {_status_label(item.status)}. "
            f"{item.mail_count} mail; {item.completed_outputs} newly saved outputs; "
            f"{item.previously_archived_outputs} previously archived outputs; "
            f"{item.failed_outputs} failed outputs."
        )
        if item.pending_outputs:
            summary += f" {item.pending_outputs} pending outputs."
        if detail.error:
            summary += f" Error: {detail.error}"
        self.detail_summary.set(summary)
        if detail.sources:
            sources_row = self.result_tree.insert("", "end", text="Selected mailboxes", open=True)
            for index, source in enumerate(detail.sources):
                self.result_tree.insert(
                    sources_row,
                    "end",
                    iid=f"source:{index}",
                    text=source.address or source.source_id,
                    values=(_status_label(source.status), "", source.error or ""),
                )
        if detail.attempts:
            attempts_row = self.result_tree.insert("", "end", text="Scan attempts", open=True)
            for index, attempt in enumerate(detail.attempts):
                attempt_row = self.result_tree.insert(
                    attempts_row,
                    "end",
                    iid=f"attempt:{index}",
                    text=f"Attempt {attempt.number} · {attempt.started_at}",
                    values=(_status_label(attempt.status), "", attempt.error or ""),
                    open=bool(attempt.error),
                )
                for source_index, source in enumerate(attempt.sources):
                    self.result_tree.insert(
                        attempt_row,
                        "end",
                        iid=f"attempt:{index}:source:{source_index}",
                        text=source.address or source.source_id,
                        values=(_status_label(source.status), "", source.error or ""),
                    )
        for mail_index, mail in enumerate(detail.mail):
            label = mail.subject or mail.address or mail.source_id
            mail_row = self.result_tree.insert(
                "",
                "end",
                iid=f"mail:{mail_index}",
                text=label,
                values=(_status_label(mail.status), "", mail.error or ""),
                open=True,
            )
            for output_index, output in enumerate(mail.outputs):
                output_label = (
                    "Previously archived"
                    if output.previously_archived
                    else "Saved output"
                    if output.can_open
                    else "Output"
                )
                output_status = (
                    "Previously archived"
                    if output.previously_archived
                    else _status_label(output.status)
                )
                row = self.result_tree.insert(
                    mail_row,
                    "end",
                    iid=f"output:{mail_index}:{output_index}",
                    text=output_label,
                    values=(
                        output_status,
                        output.final_path or output.requested_path,
                        output.error or "",
                    ),
                    open=output.previously_archived,
                )
                self._output_by_row[row] = output
                if output.previously_archived and output.completed_at:
                    self.result_tree.insert(
                        row,
                        "end",
                        iid=f"output:{mail_index}:{output_index}:archived",
                        text=f"Originally archived · {output.completed_at}",
                    )
                for attempt_index, attempt in enumerate(output.attempts):
                    self.result_tree.insert(
                        row,
                        "end",
                        iid=f"output:{mail_index}:{output_index}:attempt:{attempt_index}",
                        text=f"Attempt {attempt.number} · {attempt.started_at}",
                        values=(
                            _status_label(attempt.status),
                            attempt.final_path,
                            attempt.error or "",
                        ),
                    )

    def _select_result(self, _event=None) -> None:
        selection = self.result_tree.selection()
        self._selected_output = self._output_by_row.get(selection[0]) if selection else None
        self.open_button.configure(
            state="normal"
            if self._selected_output and self._selected_output.can_open
            else "disabled"
        )

    def stop_selected(self) -> None:
        item = self._selected_item()
        if item is None or not item.can_stop:
            return
        if not messagebox.askyesno(
            "Stop this operation?",
            "Stop this operation, including its remaining mail and outputs? Completed files remain saved.",
            parent=self,
        ):
            return
        try:
            self.application.stop_operation(item.key)
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Could not stop operation", str(exc), parent=self)

    def retry_selected(self) -> None:
        item = self._selected_item()
        if item is None or not item.can_retry:
            return
        try:
            self.application.retry_activity(item.key)
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Could not retry archive work", str(exc), parent=self)

    def open_selected(self) -> None:
        output = self._selected_output
        if output is None or not output.can_open:
            return
        path = Path(output.final_path)
        try:
            if not path.is_file():
                raise FileNotFoundError(f"Saved output is unavailable: {path}")
            self.open_output_path(path)
        except Exception as exc:
            messagebox.showerror("Could not open output", str(exc), parent=self)

    def _schedule_refresh(self) -> None:
        if self.winfo_exists():
            self._refresh_timer = self.after(3000, self._tick)

    def _tick(self) -> None:
        self._refresh_timer = None
        if self.winfo_exists():
            try:
                current = self.application.current_jobs()
            except Exception:
                self._schedule_refresh()
                return
            previous_keys = {item.key for item in self.current_items}
            self.current_items = current
            self._fill(self.current_tree, current)
            self.empty_current.set("No archive jobs are running." if not current else "")
            if (
                previous_keys - {item.key for item in current}
                and len(self.history_items) <= PAGE_SIZE
            ):
                self.refresh()
            else:
                self._update_selection()
            self._schedule_refresh()

    def destroy(self) -> None:
        if self._refresh_timer is not None:
            self.after_cancel(self._refresh_timer)
            self._refresh_timer = None
        super().destroy()
