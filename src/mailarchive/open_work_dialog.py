"""Open work UI for durable plans, range runs, and intake errors."""

from __future__ import annotations

import json
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from mailarchive.service import ArchiveService, EventLevel, ServiceEvent
from mailarchive.workspace import WorkspaceStore

INTAKE_PAGE_SIZE = 100


class OpenWorkDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        state: WorkspaceStore,
        service: ArchiveService,
        event_handler: Callable[[ServiceEvent], None],
        post_ui: Callable[[Callable[[], None]], None],
        refresh_application: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.service = service
        self.event_handler = event_handler
        self.post_ui = post_ui
        self.refresh_application = refresh_application
        self.plans = []
        self.runs = []
        self.intakes = []
        self.intake_total = 0
        self.title("Open archive work")
        self.geometry("1100x700")
        self.transient(parent)
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Accepted plans and destination status").pack(anchor="w")
        self.work_list = tk.Listbox(frame, exportselection=False)
        self.work_list.pack(fill="both", expand=True)
        self.detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.detail, wraplength=1040).pack(anchor="w", pady=6)
        ttk.Label(frame, text="Interrupted range searches").pack(anchor="w", pady=(12, 0))
        self.run_list = tk.Listbox(frame, exportselection=False, height=4)
        self.run_list.pack(fill="x")
        ttk.Label(frame, text="Unresolved message intake errors").pack(anchor="w", pady=(12, 0))
        self.intake_list = tk.Listbox(frame, exportselection=False, height=4)
        self.intake_list.pack(fill="x")
        intake_controls = ttk.Frame(frame)
        intake_controls.pack(fill="x", pady=(4, 0))
        self.intake_summary = tk.StringVar()
        ttk.Label(intake_controls, textvariable=self.intake_summary).pack(side="left")
        self.more_intakes_button = ttk.Button(
            intake_controls, text="Load more", command=self.load_more_intakes
        )
        self.more_intakes_button.pack(side="right")
        self.work_list.bind("<<ListboxSelect>>", self.show_plan)
        self.run_list.bind("<<ListboxSelect>>", self.show_run)
        self.intake_list.bind("<<ListboxSelect>>", self.show_intake)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        for column, (label, command) in enumerate(
            (
                ("Refresh", self.refresh),
                ("Pause plan", self.pause_selected),
                ("Resume plan", self.resume_selected_plan),
                ("Abort plan", self.abort_selected),
                ("Resume all", self.resume_all),
                ("Resume range", self.resume_selected_range),
                ("Cancel range", self.cancel_selected_range),
                ("Cancel intake", self.cancel_selected_intake),
            )
        ):
            ttk.Button(buttons, text=label, command=command).grid(
                row=column // 4, column=column % 4, padx=(0, 8), pady=3, sticky="ew"
            )
        ttk.Button(buttons, text="Close", command=self.destroy).grid(
            row=2, column=3, padx=(0, 8), pady=3, sticky="ew"
        )
        for column in range(4):
            buttons.columnconfigure(column, weight=1)
        self.refresh()

    def refresh(self) -> None:
        self.plans = self.state.work_plans()
        self.runs = self.state.incomplete_manual_runs()
        intakes, self.intake_total = self.state.intake_error_snapshot(INTAKE_PAGE_SIZE)
        self.intakes = list(intakes)
        self.work_list.delete(0, "end")
        self.run_list.delete(0, "end")
        self.intake_list.delete(0, "end")
        for plan in self.plans:
            targets = self.state.plan_targets(plan["id"])
            complete = sum(item["status"] in {"done", "no_output"} for item in targets)
            rule = json.loads(plan["rule_json"])["rule"]["name"]
            self.work_list.insert(
                "end",
                f"{plan['created_at']}  {rule}  {plan['status']}  "
                f"{complete}/{len(targets)} destinations  {plan['source_id']}",
            )
        for run in self.runs:
            checkpoint = json.loads(run["checkpoint"] or "{}")
            selection = json.loads(run["selection_json"] or "{}")
            last_id = checkpoint.get("last_remote_id", "")
            timezone_name = selection.get("timezone", "")
            self.run_list.insert(
                "end",
                f"{run['started_at']}  {run['source_id']}  {run['status']}"
                + (f"  last ID: {last_id}" if last_id else "")
                + (f"  timezone: {timezone_name}" if timezone_name else ""),
            )
        self._insert_intakes(self.intakes)
        self._update_intake_controls()
        self.detail.set("")

    def _insert_intakes(self, intakes) -> None:
        for intake in intakes:
            self.intake_list.insert(
                "end",
                f"{intake['created_at']}  {intake['source_id']}  "
                f"{intake['subject'] or intake['remote_id']}: {intake['error']}",
            )

    def _update_intake_controls(self) -> None:
        self.intake_summary.set(
            f"Showing {len(self.intakes)} of {self.intake_total} unresolved intake errors"
        )
        self.more_intakes_button.configure(
            state="normal" if len(self.intakes) < self.intake_total else "disabled"
        )

    def load_more_intakes(self) -> None:
        if not self.intakes or len(self.intakes) >= self.intake_total:
            return
        last = self.intakes[-1]
        page = list(
            self.state.intake_errors(
                INTAKE_PAGE_SIZE,
                before=(str(last["created_at"]), str(last["id"])),
            )
        )
        if not page:
            # Items can be resolved while this dialog is open. Rebase the
            # snapshot instead of leaving an enabled button that can never
            # make progress.
            self.refresh()
            return
        self.intakes.extend(page)
        self._insert_intakes(page)
        self._update_intake_controls()

    def _refresh_if_open(self) -> None:
        try:
            if self.winfo_exists():
                self.refresh()
        except tk.TclError:
            return

    def show_plan(self, _event=None) -> None:
        selected = self.work_list.curselection()
        if not selected:
            return
        plan = self.plans[selected[0]]
        lines = [f"Plan status: {plan['status']}"]
        if plan["error"]:
            lines.append(f"Plan error: {plan['error']}")
        for target in self.state.plan_targets(plan["id"]):
            lines.append(
                f"Destination {target['status']}: {target['path']}"
                + (f" ({target['error']})" if target["error"] else "")
            )
        for output in self.state.outputs(plan["id"]):
            lines.append(
                f"  Output {output['status']}: {output['final_path']}"
                + (f" ({output['error']})" if output["error"] else "")
            )
        self.detail.set("\n".join(lines))

    def show_run(self, _event=None) -> None:
        selected = self.run_list.curselection()
        if selected:
            run = self.runs[selected[0]]
            self.detail.set(run["error"] or "The range run can be resumed.")

    def show_intake(self, _event=None) -> None:
        selected = self.intake_list.curselection()
        if selected:
            intake = self.intakes[selected[0]]
            self.detail.set(
                f"Message {intake['remote_id']} from source {intake['source_id']}: "
                f"{intake['error']}"
            )

    def selected_plan(self):
        selected = self.work_list.curselection()
        return self.plans[selected[0]] if selected else None

    def pause_selected(self) -> None:
        plan = self.selected_plan()
        if plan is None:
            return
        try:
            self.service.pause_plan(plan["id"])
        except Exception as exc:
            messagebox.showerror("Could not pause plan", str(exc), parent=self)
            return
        self.refresh()
        self.refresh_application()

    def resume_selected_plan(self) -> None:
        plan = self.selected_plan()
        if plan is None:
            return
        self._background_plan_resume(plan["id"])

    def _background_plan_resume(self, plan_id: str) -> None:
        def work() -> None:
            try:
                done, failed = self.service.resume_plan(plan_id)
                self.event_handler(
                    ServiceEvent(
                        EventLevel.WARNING if failed else EventLevel.SUCCESS,
                        f"Plan resumed: {done} outputs completed, {failed} still failed.",
                    )
                )
            except Exception as exc:
                self.event_handler(ServiceEvent(EventLevel.ERROR, f"Could not resume plan: {exc}"))
            self.post_ui(self._refresh_if_open)
            self.post_ui(self.refresh_application)

        threading.Thread(target=work, name="MailArchive-Plan-Resume", daemon=True).start()

    def abort_selected(self) -> None:
        plan = self.selected_plan()
        if plan is None or not messagebox.askyesno(
            "Abort this plan?",
            "Remaining outputs will be abandoned. Completed files stay in place.",
            parent=self,
        ):
            return
        try:
            self.service.abort_plan(plan["id"])
        except Exception as exc:
            messagebox.showerror("Could not abort plan", str(exc), parent=self)
            return
        self.refresh()
        self.refresh_application()

    def resume_all(self) -> None:
        def work() -> None:
            try:
                done, failed = self.service.resume_open()
                self.event_handler(
                    ServiceEvent(
                        EventLevel.WARNING if failed else EventLevel.SUCCESS,
                        f"Open work: {done} outputs completed, {failed} still failed.",
                    )
                )
            except Exception as exc:
                self.event_handler(ServiceEvent(EventLevel.ERROR, f"Could not resume work: {exc}"))
            self.post_ui(self._refresh_if_open)
            self.post_ui(self.refresh_application)

        threading.Thread(target=work, name="MailArchive-Resume", daemon=True).start()

    def resume_selected_range(self) -> None:
        selected = self.run_list.curselection()
        if not selected:
            return
        run_id = self.runs[selected[0]]["id"]

        def work() -> None:
            try:
                result = self.service.resume_range_run(run_id)
                self.event_handler(
                    ServiceEvent(
                        EventLevel.WARNING if result.failed else EventLevel.SUCCESS,
                        f"Resumed range: {result.archived} messages with new outputs; "
                        f"{result.failed} failures.",
                    )
                )
            except Exception as exc:
                self.event_handler(ServiceEvent(EventLevel.ERROR, f"Could not resume range: {exc}"))
            self.post_ui(self._refresh_if_open)
            self.post_ui(self.refresh_application)

        threading.Thread(target=work, name="MailArchive-Range-Resume", daemon=True).start()

    def cancel_selected_range(self) -> None:
        selected = self.run_list.curselection()
        if not selected or not messagebox.askyesno(
            "Cancel this range run?",
            "Unaccepted message downloads from this run will be abandoned. "
            "Accepted plans and completed files remain available.",
            parent=self,
        ):
            return
        self.service.cancel_run(self.runs[selected[0]]["id"])
        self.refresh()
        self.refresh_application()

    def cancel_selected_intake(self) -> None:
        selected = self.intake_list.curselection()
        if not selected or not messagebox.askyesno(
            "Cancel this message intake?",
            "The unresolved provider message will be abandoned. Processing history is kept.",
            parent=self,
        ):
            return
        try:
            self.service.cancel_intake(self.intakes[selected[0]]["id"])
        except Exception as exc:
            messagebox.showerror("Could not cancel message intake", str(exc), parent=self)
            return
        self.refresh()
        self.refresh_application()
