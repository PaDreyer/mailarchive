"""Linux-only desktop setup UI; filesystem work never runs on the Tk thread."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from mailarchive import __version__
from mailarchive.application.background import BackgroundResult
from mailarchive.application.desktop_integration import (
    DesktopIntegrationPort,
    IntegrationError,
    IntegrationOptions,
    IntegrationResult,
    IntegrationStatus,
)
from mailarchive.presentation.dialogs import _center_on_parent, _wrap_label_to_width
from mailarchive.presentation.scrollable_frame import ScrollableFrame


class DesktopIntegrationDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        status: IntegrationStatus,
        *,
        initial: bool,
        submit: Callable[[IntegrationOptions], None],
        cancel: Callable[[], None],
    ) -> None:
        desktop_available = status.desktop_available
        state = status.state
        super().__init__(parent)
        self.withdraw()
        self.title("Set up MailArchive" if initial else "Desktop integration")
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", cancel)
        self.bind("<Escape>", lambda event: cancel())
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.form_scroll = ScrollableFrame(frame)
        self.form_scroll.grid(row=0, column=0, sticky="nsew")
        body = self.form_scroll.content
        labels = []
        explanation = (
            "Shortcuts and enabled login autostart will use the installed copy. "
            "Settings and archived emails are not changed."
        )
        if not initial:
            explanation += (
                " Clear both options to remove managed shortcuts; the installed "
                "application and autostart are kept."
            )
        explanation += " Start at login is controlled by General settings."
        introduction = ttk.Label(
            body,
            text=(
                f"Set up MailArchive {__version__} for your user account.\n"
                "No administrator access is needed. The downloaded file is left unchanged."
            ),
            wraplength=560,
            justify="left",
        )
        introduction.pack(fill="x")
        labels.append(introduction)
        description = ttk.Label(
            body,
            text=(
                "If a shortcut is selected, the running AppImage is copied here, replacing "
                "any version previously installed by MailArchive:\n"
                f"{status.application_path}\n"
                f"Currently installed version: {state.installed_version or 'None'}\n\n"
                + explanation
            ),
            wraplength=560,
            justify="left",
        )
        description.pack(fill="x", pady=(12, 14))
        labels.append(description)
        options = state.options if state.installed_version else IntegrationOptions()
        self.menu = tk.BooleanVar(value=options.menu_entry)
        self.desktop = tk.BooleanVar(value=options.desktop_shortcut and desktop_available)
        menu = ttk.Checkbutton(body, text="Add to the application menu", variable=self.menu)
        menu.pack(anchor="w", pady=4)
        desktop = ttk.Checkbutton(body, text="Create a desktop shortcut", variable=self.desktop)
        desktop.pack(anchor="w", pady=4)
        if not desktop_available:
            desktop.configure(state="disabled")
        desktop_hint = ttk.Label(
            body,
            text=(
                "Your desktop environment may hide desktop icons or require Allow Launching."
                if desktop_available
                else "No usable desktop folder is configured. The application menu still works."
            ),
            wraplength=560,
            justify="left",
        )
        desktop_hint.pack(fill="x", pady=(4, 12))
        labels.append(desktop_hint)
        footer = ttk.Frame(frame)
        footer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        progress_area = ttk.Frame(footer, height=16)
        progress_area.pack(fill="x")
        progress_area.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_area, mode="indeterminate")
        self.status = tk.StringVar(value="Choose shortcuts, or continue without setup.")
        status_label = ttk.Label(footer, textvariable=self.status, wraplength=560)
        status_label.pack(fill="x", pady=6)
        labels.append(status_label)
        buttons = ttk.Frame(footer)
        buttons.pack(anchor="e", pady=(8, 0))
        skip = ttk.Button(
            buttons, text="Only run, without setup" if initial else "Cancel", command=cancel
        )
        skip.pack(side="left", padx=(0, 8))
        apply = ttk.Button(
            buttons,
            text="Apply",
            command=lambda: submit(IntegrationOptions(self.menu.get(), self.desktop.get())),
        )
        apply.pack(side="left")
        self.controls = [menu, skip, apply] + ([desktop] if desktop_available else [])
        self.update_idletasks()
        horizontal_chrome = (
            2 * frame.winfo_pixels(frame.cget("padding")[0])
            + self.form_scroll.winfo_reqwidth()
            - self.form_scroll.canvas.winfo_reqwidth()
        )
        width = min(
            max(body.winfo_reqwidth(), footer.winfo_reqwidth()) + horizontal_chrome,
            self.winfo_screenwidth() - 48,
        )
        self.form_scroll.canvas.configure(width=width - horizontal_chrome)
        for label in labels:
            label.configure(wraplength=width - horizontal_chrome)
        self.form_scroll.bind_widgets()
        self.update_idletasks()
        footer_height = frame.winfo_reqheight() - self.form_scroll.winfo_reqheight()
        height = min(body.winfo_reqheight() + footer_height, self.winfo_screenheight() - 80)
        self.form_scroll.canvas.configure(height=max(1, height - footer_height))
        _center_on_parent(self, parent, width=width, height=height, keep_visible=True)
        for label in labels:
            _wrap_label_to_width(label)
        self.deiconify()
        self.update_idletasks()
        self.grab_set()
        apply.focus_set()

    def destroy(self) -> None:
        self.progress.stop()
        super().destroy()

    def set_busy(self, busy: bool) -> None:
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        if busy:
            self.status.set("Applying desktop integration. Please wait...")
            self.progress.pack(fill="both", expand=True)
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.pack_forget()


class DesktopIntegrationUI:
    def __init__(
        self,
        root: tk.Misc,
        integration: DesktopIntegrationPort,
        start_at_login: Callable[[], bool],
        submit_background: Callable[
            [Callable[[], IntegrationResult], Callable[[BackgroundResult], None]], None
        ],
    ) -> None:
        self.root = root
        self.integration = integration
        self.start_at_login = start_at_login
        self.submit_background = submit_background
        self.dialog: DesktopIntegrationDialog | None = None
        self.busy = False
        self.initial = False
        self.summary: tk.StringVar | None = None

    def add_settings_page(self, notebook: ttk.Notebook) -> None:
        container = ttk.Frame(notebook, padding=16)
        notebook.add(container, text="Desktop integration")
        self.settings_scroll = ScrollableFrame(container)
        self.settings_scroll.pack(fill="both", expand=True)
        page = self.settings_scroll.content
        introduction = ttk.Label(
            page,
            text="Manage the local AppImage installation and its shortcuts.",
            wraplength=660,
        )
        introduction.pack(fill="x")
        _wrap_label_to_width(introduction)
        self.summary = tk.StringVar()
        summary_label = ttk.Label(page, textvariable=self.summary, wraplength=660, justify="left")
        summary_label.pack(fill="x", pady=16)
        _wrap_label_to_width(summary_label)
        ttk.Button(page, text="Configure", command=self.configure).pack(anchor="w")
        update_hint = ttk.Label(
            page,
            text=(
                "To update an integrated installation, quit MailArchive, start the new "
                "AppImage and choose Configure > Apply here. Then quit and reopen MailArchive "
                "from its shortcut. Clearing both shortcut options does not uninstall the "
                "application or remove your data."
            ),
            wraplength=660,
            justify="left",
        )
        update_hint.pack(fill="x", pady=16)
        _wrap_label_to_width(update_hint)
        self.settings_scroll.bind_widgets()
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if self.summary is None:
            return
        try:
            status = self.integration.status()
        except (OSError, IntegrationError) as exc:
            self.summary.set(f"Could not read desktop integration: {exc}")
            return
        state = status.state
        installed = state.installed_version or "Not installed"
        if state.installed_version and not status.application_present:
            installed += " (application file missing)"
        self.summary.set(
            f"Running version: {__version__}\nInstalled version: {installed}\n"
            f"Application: {status.application_path}\n"
            f"Application menu: {'Enabled' if state.menu_entry else 'Disabled'}\n"
            f"Desktop shortcut: {state.desktop_path or 'Disabled'}"
        )

    def offer_once(self) -> None:
        try:
            if not self.integration.status().state.prompt_seen:
                self.root.after_idle(lambda: self.configure(initial=True))
        except (OSError, IntegrationError) as exc:
            messagebox.showerror("Desktop integration unavailable", str(exc), parent=self.root)

    def configure(self, *, initial: bool = False) -> None:
        if self.dialog is not None:
            self.dialog.lift()
            return
        try:
            status = self.integration.status()
            self.dialog = DesktopIntegrationDialog(
                self.root,
                status,
                initial=initial,
                submit=self._submit,
                cancel=self._cancel,
            )
            self.initial = initial
        except (OSError, IntegrationError, ValueError) as exc:
            messagebox.showerror("Desktop integration unavailable", str(exc), parent=self.root)

    def _cancel(self) -> None:
        if self.busy or self.dialog is None:
            return
        if self.initial:
            try:
                self.integration.mark_prompt_seen()
            except (OSError, IntegrationError) as exc:
                messagebox.showerror("Could not save setup choice", str(exc), parent=self.dialog)
        self.dialog.destroy()
        self.dialog = None
        self._refresh_summary()

    def _submit(self, options: IntegrationOptions) -> None:
        if self.busy or self.dialog is None:
            return
        self.busy = True
        self.dialog.set_busy(True)
        start_at_login = self.start_at_login()

        try:
            self.submit_background(
                lambda: self.integration.apply(options, start_at_login=start_at_login),
                lambda result: self._finish(
                    result.value if result.error is None else None,
                    error=str(result.error) if result.error is not None else "",
                ),
            )
        except Exception as exc:
            self._finish(error=str(exc))

    def _finish(self, result: IntegrationResult | None = None, *, error: str = "") -> None:
        self.busy = False
        if self.dialog is None:
            return
        self.dialog.set_busy(False)
        if error:
            self.dialog.status.set("Setup failed. You can retry or continue without setup.")
            messagebox.showerror("Desktop integration failed", error, parent=self.dialog)
            return
        self.dialog.status.set("Desktop integration saved.")
        text = "Desktop integration saved."
        if result is not None and result.state.installed_version:
            text += "\nQuit and reopen MailArchive to use the installed copy."
        if result is not None and result.warnings:
            text += "\n\n" + "\n".join(result.warnings)
        messagebox.showinfo("MailArchive", text, parent=self.dialog)
        self.dialog.destroy()
        self.dialog = None
        self._refresh_summary()

    def show_busy(self) -> None:
        if self.dialog is not None:
            self.dialog.lift()
        messagebox.showinfo(
            "Desktop integration in progress",
            "Please wait for desktop integration to finish before closing MailArchive.",
            parent=self.dialog or self.root,
        )
