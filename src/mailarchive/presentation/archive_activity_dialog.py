"""Archive activity view backed by typed application read models."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import font, messagebox, ttk

from mailarchive.application.activity import (
    ActivityDetail,
    ActivityItem,
    ActivityPage,
    OutputResult,
)
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.application.session import MailArchiveApplication
from mailarchive.presentation.dialogs import _center_on_parent

PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class _DetailView:
    expanded: dict[str, bool]
    selected: tuple[str, ...]
    focus: str
    anchor: str | None
    first_row: int
    follow: bool
    horizontal: float
    summary: float


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
        self.withdraw()
        self.application = application
        self.open_output_path = open_output_path
        self.title("Archive activity")
        self.transient(parent)
        style = ttk.Style(self)
        row_font = font.Font(root=self, font=style.lookup("Treeview", "font") or "TkDefaultFont")
        row_height = row_font.metrics("linespace") + 8
        style.configure("ArchiveActivity.Treeview", rowheight=row_height)
        self.current_items: tuple[ActivityItem, ...] = ()
        self.history_items: list[ActivityItem] = []
        self._next_before: tuple[str, str] | None = None
        self._selected_output: OutputResult | None = None
        self._detail_key: str | None = None
        self._detail_view: _DetailView | None = None
        self._refresh_timer: str | None = None
        self._profile_path = application.database_path
        self._history_floor: tuple[str, str] | None = None
        self._history_expanded = False

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Archive activity", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Current jobs and past mail results, including saved files and errors.",
        ).pack(anchor="w", pady=(3, 10))

        panes = self.panes = ttk.Panedwindow(frame, orient="vertical")
        panes.pack(fill="both", expand=True)
        lists = ttk.Panedwindow(panes, orient="horizontal")
        panes.add(lists, weight=1)
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
        ttk.Label(current_frame, textvariable=self.empty_current).pack(
            side="bottom", anchor="w", pady=(4, 0), before=self.current_tree.master
        )
        history_controls = ttk.Frame(history_frame)
        history_controls.pack(side="bottom", fill="x", pady=(4, 0), before=self.history_tree.master)
        self.history_summary = tk.StringVar(value="")
        ttk.Label(history_controls, textvariable=self.history_summary).pack(side="left")
        self.more_button = ttk.Button(history_controls, text="Load more", command=self.load_more)
        self.more_button.pack(side="right")

        detail_frame = ttk.LabelFrame(panes, text="Selected job", padding=8)
        panes.add(detail_frame, weight=1)
        self.detail_summary = tk.StringVar(value="Select a job to see its mail and outputs.")
        summary_frame = ttk.Frame(detail_frame)
        summary_frame.pack(fill="x")
        self.detail_text = tk.Text(
            summary_frame,
            height=2,
            width=1,
            wrap="word",
            state="disabled",
            font="TkDefaultFont",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        summary_scroll = ttk.Scrollbar(
            summary_frame, orient="vertical", command=self.detail_text.yview
        )
        summary_scroll.grid(row=0, column=1, sticky="ns")
        self.detail_text.configure(yscrollcommand=summary_scroll.set)
        summary_frame.columnconfigure(0, weight=1)
        self._set_detail_summary(self.detail_summary.get(), force=True)
        result_table = ttk.Frame(detail_frame)
        result_table.pack(fill="both", expand=True)
        self.result_tree = ttk.Treeview(
            result_table,
            columns=("status", "path", "error"),
            show="tree headings",
            height=8,
            style="ArchiveActivity.Treeview",
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
        # Reserve the footer before expandable panes, including wrapped errors.
        controls.pack(side="bottom", fill="x", pady=(10, 0), before=panes)
        ttk.Button(controls, text="Refresh", command=self.refresh).grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(
            controls, text="Stop selected job", command=self.stop_selected
        )
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.retry_button = ttk.Button(
            controls, text="Retry selected failure", command=self.retry_selected
        )
        self.retry_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.open_button = ttk.Button(
            controls, text="Open selected output", command=self.open_selected
        )
        self.open_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(controls, text="Close", command=self.destroy).grid(
            row=1, column=2, sticky="e", pady=(6, 0)
        )
        controls.columnconfigure(2, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.refresh()
        _center_on_parent(
            self,
            parent,
            width=min(1220, self.winfo_screenwidth() - 48),
            height=min(760, self.winfo_screenheight() - 80),
            keep_visible=True,
        )
        self.deiconify()
        self.update_idletasks()
        self._position_initial_sash(
            panes, lists, current_frame, history_frame, detail_frame, row_height
        )
        self._schedule_refresh()

    def _position_initial_sash(
        self, panes, lists, current_frame, history_frame, detail_frame, row_height
    ) -> None:
        def minimum_height(frame, tree):
            header = tree.winfo_reqheight() - int(tree.cget("height")) * row_height
            chrome = frame.winfo_height() - tree.winfo_height()
            return chrome + header + row_height

        minimum_lists = max(
            minimum_height(current_frame, self.current_tree),
            minimum_height(history_frame, self.history_tree),
        )
        minimum_details = minimum_height(detail_frame, self.result_tree)
        sash = panes.winfo_height() - lists.winfo_height() - detail_frame.winfo_height()
        maximum_lists = panes.winfo_height() - sash - minimum_details
        position = round(panes.winfo_height() * 0.4)
        if minimum_lists <= maximum_lists:
            position = max(minimum_lists, min(position, maximum_lists))
        panes.sashpos(0, position)

    @staticmethod
    def _make_list(parent: ttk.Frame, columns: tuple[str, ...]) -> ttk.Treeview:
        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            table, columns=columns, show="headings", height=9, style="ArchiveActivity.Treeview"
        )
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
        first = tree.yview()[0]
        anchor = (
            children[min(round(first * len(children)), len(children) - 1)]
            if children and first
            else None
        )
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
        if anchor and tree.exists(anchor):
            tree.yview_moveto(tree.index(anchor) / max(1, len(items)))
        elif not first:
            tree.yview_moveto(0)

    def _reset_changed_profile(self) -> None:
        profile_path = self.application.database_path
        if profile_path == self._profile_path:
            return
        self._profile_path = profile_path
        self.current_items = ()
        self.history_items = []
        self._next_before = None
        self._history_floor = None
        self._history_expanded = False
        self._detail_key = None
        self._detail_view = None
        self._fill(self.current_tree, ())
        self._fill(self.history_tree, ())
        self._update_history_controls()
        self._update_selection()

    def _show_history(self, page: ActivityPage) -> None:
        if page.items != tuple(self.history_items):
            self.history_items = list(page.items)
            self._fill(self.history_tree, self.history_items)
        self._next_before = page.next_before
        self._update_history_controls()

    def _read_history(self) -> ActivityPage:
        reading_older = (
            self._history_expanded
            or bool(self.history_tree.selection())
            or self.history_tree.yview()[0] > 0
        )
        floor = self._history_floor if reading_older else None
        page = self.application.activity_page(
            limit=max(PAGE_SIZE, len(self.history_items)) if floor else PAGE_SIZE
        )
        if floor is None:
            self._history_floor = (
                (page.items[-1].occurred_at, page.items[-1].key) if page.items else None
            )
            return page
        items = list(page.items)
        while page.next_before and items and (items[-1].occurred_at, items[-1].key) > floor:
            page = self.application.activity_page(before=page.next_before, limit=PAGE_SIZE)
            items.extend(page.items)
        # Retain the user's loaded range without growing its lower boundary as
        # new jobs arrive. Keyset pages can include older rows past that boundary.
        visible = tuple(item for item in items if (item.occurred_at, item.key) >= floor)
        has_more = page.next_before is not None or len(visible) < len(items)
        cursor = (visible[-1].occurred_at, visible[-1].key) if visible else floor
        return ActivityPage(visible, cursor if has_more else None)

    def _profile_available(self) -> bool:
        return self.application.automatic_monitoring_state() != AutomaticMonitoringState.UNAVAILABLE

    def _pause_profile_reading(self) -> None:
        self.more_button.configure(state="disabled")
        self._update_selection()
        self._set_detail_summary(
            "The profile is temporarily unavailable. Wait for the change, or select another database in Settings."
        )

    def refresh(self) -> None:
        if not self._profile_available():
            self._pause_profile_reading()
            return
        self._reset_changed_profile()
        selected = self._selected_item()
        try:
            current = self.application.current_jobs()
            page = self._read_history()
        except Exception as exc:
            messagebox.showerror("Could not load archive activity", str(exc), parent=self)
            return
        self._show_jobs(current, page, selected)

    def _show_jobs(
        self, current: tuple[ActivityItem, ...], page: ActivityPage, selected: ActivityItem | None
    ) -> None:
        self.current_items = current
        self._fill(self.current_tree, current)
        if selected is not None and not any(item.key == selected.key for item in current):
            if not any(item.key == selected.key for item in page.items):
                try:
                    # A long-running job can finish below the loaded history floor.
                    completed = self.application.activity_detail(selected.key).item
                except Exception:
                    pass
                else:
                    items = tuple(
                        sorted(
                            (*page.items, completed),
                            key=lambda item: (item.occurred_at, item.key),
                            reverse=True,
                        )
                    )
                    page = ActivityPage(items, page.next_before)
        self._show_history(page)
        if selected is not None:
            selected_tree = (
                self.current_tree if self.current_tree.exists(selected.key) else self.history_tree
            )
            for tree in (self.current_tree, self.history_tree):
                if tree is selected_tree and tree.exists(selected.key):
                    tree.selection_set(selected.key)
                elif tree.selection():
                    tree.selection_remove(*tree.selection())
        self.empty_current.set("No archive jobs are running." if not current else "")
        self._update_selection()

    def load_more(self) -> None:
        if not self._profile_available():
            self._pause_profile_reading()
            return
        if self.application.database_path != self._profile_path:
            self.refresh()
            return
        if self._next_before is None:
            return
        try:
            page = self.application.activity_page(before=self._next_before, limit=PAGE_SIZE)
        except Exception as exc:
            messagebox.showerror("Could not load history", str(exc), parent=self)
            return
        items = {item.key: item for item in (*self.history_items, *page.items)}
        self.history_items = sorted(
            items.values(), key=lambda item: (item.occurred_at, item.key), reverse=True
        )
        self._history_expanded = True
        if page.items:
            self._history_floor = page.items[-1].occurred_at, page.items[-1].key
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
        if not self._profile_available():
            return None
        if self.application.database_path != self._profile_path:
            return None
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
        if self._detail_key is not None and self.result_tree.get_children():
            self._detail_view = self._capture_detail_view()
        if item is not None and not same_job:
            self._detail_view = None
        self.stop_button.configure(state="normal" if item and item.can_stop else "disabled")
        self.retry_button.configure(state="normal" if item and item.can_retry else "disabled")
        self._selected_output = None
        self.open_button.configure(state="disabled")
        self._output_by_row.clear()
        self.result_tree.delete(*self.result_tree.get_children())
        if item is None:
            if self._profile_available():
                self._detail_key = None
                self._detail_view = None
            self._set_detail_summary("Select a job to see its mail and outputs.")
            return
        self._detail_key = item.key
        if not same_job:
            self.detail_text.yview_moveto(0)
        try:
            detail = self.application.activity_detail(item.key)
        except Exception as exc:
            self._set_detail_summary(f"Could not load job details: {exc}")
            return
        self._show_detail(detail)
        if self._detail_view is not None:
            self._restore_detail_view(self._detail_view)
        else:
            self.result_tree.yview_moveto(0)
            self.result_tree.xview_moveto(0)
        self._detail_view = self._capture_detail_view()

    def _result_rows(self, parent: str = "", *, visible_only: bool = False) -> list[str]:
        rows = []
        for row in self.result_tree.get_children(parent):
            rows.append(row)
            if not visible_only or self.result_tree.item(row, "open"):
                rows.extend(self._result_rows(row, visible_only=visible_only))
        return rows

    def _capture_detail_view(self) -> _DetailView:
        rows = self._result_rows(visible_only=True)
        first, last = self.result_tree.yview()
        first_row = round(first * len(rows))
        return _DetailView(
            {row: bool(self.result_tree.item(row, "open")) for row in self._result_rows()},
            self.result_tree.selection(),
            self.result_tree.focus(),
            rows[min(first_row, len(rows) - 1)] if rows else None,
            first_row,
            last >= 1.0,
            self.result_tree.xview()[0],
            self.detail_text.yview()[0],
        )

    def _restore_detail_view(self, view: _DetailView) -> None:
        tree = self.result_tree
        for row, expanded in view.expanded.items():
            if tree.exists(row):
                tree.item(row, open=expanded)
        selected = tuple(row for row in view.selected if tree.exists(row))
        if selected:
            tree.selection_set(selected)
        if tree.exists(view.focus):
            tree.focus(view.focus)
        self._select_result()
        rows = self._result_rows(visible_only=True)
        if view.follow:
            tree.yview_moveto(1.0)
        else:
            first_row = rows.index(view.anchor) if view.anchor in rows else view.first_row
            tree.yview_moveto(first_row / max(1, len(rows)))
        tree.xview_moveto(view.horizontal)
        self.detail_text.yview_moveto(view.summary)

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
        self._set_detail_summary(summary)
        if detail.sources:
            sources_row = self.result_tree.insert(
                "", "end", iid="group:sources", text="Selected mailboxes", open=True
            )
            for source in detail.sources:
                self.result_tree.insert(
                    sources_row,
                    "end",
                    iid=f"source:{source.source_id}",
                    text=source.address or source.source_id,
                    values=(_status_label(source.status), "", source.error or ""),
                )
        if detail.attempts:
            attempts_row = self.result_tree.insert(
                "", "end", iid="group:attempts", text="Scan attempts", open=True
            )
            for attempt in detail.attempts:
                attempt_row = self.result_tree.insert(
                    attempts_row,
                    "end",
                    iid=f"attempt:{attempt.number}",
                    text=f"Attempt {attempt.number} · {attempt.started_at}",
                    values=(_status_label(attempt.status), "", attempt.error or ""),
                    open=bool(attempt.error),
                )
                for source in attempt.sources:
                    self.result_tree.insert(
                        attempt_row,
                        "end",
                        iid=f"attempt:{attempt.number}:source:{source.source_id}",
                        text=source.address or source.source_id,
                        values=(_status_label(source.status), "", source.error or ""),
                    )
        for mail in detail.mail:
            label = mail.subject
            if label is None:
                label = "Loading mail…" if mail.status == "reserved" else "Subject unavailable"
            elif not label.strip() or label == "(no subject)":
                label = "<NO_SUBJECT>"
            mail_row = self.result_tree.insert(
                "",
                "end",
                iid=mail.key,
                text=label,
                values=(_status_label(mail.status), "", mail.error or ""),
                open=True,
            )
            for output in mail.outputs:
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
                    iid=f"output:{output.output_id}",
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
                        iid=f"output:{output.output_id}:archived",
                        text=f"Originally archived · {output.completed_at}",
                    )
                for attempt in output.attempts:
                    self.result_tree.insert(
                        row,
                        "end",
                        iid=f"output:{output.output_id}:attempt:{attempt.number}",
                        text=f"Attempt {attempt.number} · {attempt.started_at}",
                        values=(
                            _status_label(attempt.status),
                            attempt.final_path,
                            attempt.error or "",
                        ),
                    )

    def _set_detail_summary(self, summary: str, *, force: bool = False) -> None:
        if not force and summary == self.detail_summary.get():
            return
        first = self.detail_text.yview()[0]
        self.detail_summary.set(summary)
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", summary)
        self.detail_text.configure(state="disabled")
        self.detail_text.yview_moveto(first)

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
        if self.application.database_path != self._profile_path:
            self.refresh()
            return
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
            if not self._profile_available():
                self._pause_profile_reading()
                self._schedule_refresh()
                return
            self._reset_changed_profile()
            selected = self._selected_item()
            try:
                current = self.application.current_jobs()
                page = self._read_history()
            except Exception:
                self._schedule_refresh()
                return
            self._show_jobs(current, page, selected)
            self._schedule_refresh()

    def destroy(self) -> None:
        if self._refresh_timer is not None:
            self.after_cancel(self._refresh_timer)
            self._refresh_timer = None
        super().destroy()
