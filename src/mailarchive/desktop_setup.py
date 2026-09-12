"""Linux-only desktop setup UI; filesystem work never runs on the Tk thread."""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from mailarchive import __version__
from mailarchive.dialogs import _center_on_parent
from mailarchive.linux_integration import (
    AppImageIntegration,
    IntegrationError,
    IntegrationOptions,
    IntegrationResult,
    IntegrationState,
)


class DesktopIntegrationDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        integration: AppImageIntegration,
        state: IntegrationState,
        *,
        initial: bool,
        submit: Callable[[IntegrationOptions], None],
        cancel: Callable[[], None],
    ) -> None:
        desktop_available = integration.paths.desktop_directory() is not None
        super().__init__(parent)
        self.withdraw()
        self.title("Set up MailArchive" if initial else "Desktop integration")
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", cancel)
        self.bind("<Escape>", lambda event: cancel())
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
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
        ttk.Label(
            frame,
            text=(
                f"Set up MailArchive {__version__} for your user account.\n"
                "No administrator access is needed. The downloaded file is left unchanged."
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "If a shortcut is selected, the running AppImage is copied here, replacing "
                "any version previously installed by MailArchive:\n"
                f"{integration.paths.application}\n"
                f"Currently installed version: {state.installed_version or 'None'}\n\n"
                + explanation
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(12, 14))
        options = state.options if state.installed_version else IntegrationOptions()
        self.menu = tk.BooleanVar(value=options.menu_entry)
        self.desktop = tk.BooleanVar(value=options.desktop_shortcut and desktop_available)
        menu = ttk.Checkbutton(frame, text="Add to the application menu", variable=self.menu)
        menu.pack(anchor="w", pady=4)
        desktop = ttk.Checkbutton(frame, text="Create a desktop shortcut", variable=self.desktop)
        desktop.pack(anchor="w", pady=4)
        if not desktop_available:
            desktop.configure(state="disabled")
        ttk.Label(
            frame,
            text=(
                "Your desktop environment may hide desktop icons or require Allow Launching."
                if desktop_available
                else "No usable desktop folder is configured. The application menu still works."
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))
        progress_area = ttk.Frame(frame, height=16)
        progress_area.pack(fill="x")
        progress_area.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_area, mode="indeterminate")
        self.status = tk.StringVar(value="Choose shortcuts, or continue without setup.")
        ttk.Label(frame, textvariable=self.status, wraplength=560).pack(anchor="w", pady=6)
        buttons = ttk.Frame(frame)
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
        _center_on_parent(self, parent)
        self.deiconify()
        self.update_idletasks()
        self.grab_set()
        apply.focus_set()

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
        integration: AppImageIntegration,
        post_ui: Callable[[Callable[[], None]], None],
        start_at_login: Callable[[], bool],
    ) -> None:
        self.root = root
        self.integration = integration
        self.post_ui = post_ui
        self.start_at_login = start_at_login
        self.dialog: DesktopIntegrationDialog | None = None
        self.busy = False
        self.initial = False
        self.summary: tk.StringVar | None = None

    @classmethod
    def for_current_process(
        cls,
        root: tk.Misc,
        post_ui: Callable[[Callable[[], None]], None],
        start_at_login: Callable[[], bool],
    ) -> DesktopIntegrationUI | None:
        integration = AppImageIntegration.for_current_process()
        return cls(root, integration, post_ui, start_at_login) if integration else None

    def add_settings_page(self, notebook: ttk.Notebook) -> None:
        page = ttk.Frame(notebook, padding=16)
        notebook.add(page, text="Desktop integration")
        ttk.Label(
            page,
            text="Manage the local AppImage installation and its shortcuts.",
            wraplength=660,
        ).pack(anchor="w")
        self.summary = tk.StringVar()
        ttk.Label(page, textvariable=self.summary, wraplength=660, justify="left").pack(
            anchor="w", pady=16
        )
        ttk.Button(page, text="Configure...", command=self.configure).pack(anchor="w")
        ttk.Label(
            page,
            text=(
                "To update an integrated installation, quit MailArchive, start the new "
                "AppImage and choose Configure > Apply here. Then quit and reopen MailArchive "
                "from its shortcut. Clearing both shortcut options does not uninstall the "
                "application or remove your data."
            ),
            wraplength=660,
            justify="left",
        ).pack(anchor="w", pady=16)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if self.summary is None:
            return
        try:
            state = self.integration.load_state()
        except (OSError, IntegrationError) as exc:
            self.summary.set(f"Could not read desktop integration: {exc}")
            return
        installed = state.installed_version or "Not installed"
        if state.installed_version and not self.integration.paths.application.is_file():
            installed += " (application file missing)"
        self.summary.set(
            f"Running version: {__version__}\nInstalled version: {installed}\n"
            f"Application: {self.integration.paths.application}\n"
            f"Application menu: {'Enabled' if state.menu_entry else 'Disabled'}\n"
            f"Desktop shortcut: {state.desktop_path or 'Disabled'}"
        )

    def offer_once(self) -> None:
        try:
            if not self.integration.load_state().prompt_seen:
                self.root.after_idle(lambda: self.configure(initial=True))
        except (OSError, IntegrationError) as exc:
            messagebox.showerror("Desktop integration unavailable", str(exc), parent=self.root)

    def configure(self, *, initial: bool = False) -> None:
        if self.dialog is not None:
            self.dialog.lift()
            return
        try:
            state = self.integration.load_state()
            self.dialog = DesktopIntegrationDialog(
                self.root,
                self.integration,
                state,
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

        def install() -> None:
            try:
                result = self.integration.apply(options, start_at_login=start_at_login)
            except (OSError, IntegrationError, ValueError) as exc:
                self.post_ui(lambda error=str(exc): self._finish(error=error))
            else:
                self.post_ui(lambda: self._finish(result))

        try:
            # Do not abandon an in-progress filesystem transaction during shutdown.
            threading.Thread(target=install, name="MailArchive-DesktopSetup", daemon=False).start()
        except RuntimeError as exc:
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
