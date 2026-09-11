from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from mailarchive.account_form import visible_account_fields
from mailarchive.config import ConfigStore
from mailarchive.credentials import (
    KeyringCredentialStore,
    UnavailableCredentialStore,
    WindowsCredentialStore,
)
from mailarchive.credential_data import load_credential_data, save_credential_data
from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    MailField,
    MailProvider,
    MatchOperator,
    Rule,
    SaveMode,
    Settings,
)
from mailarchive.oauth import authorize_account, parse_google_service_account_file
from mailarchive.runner import BackgroundRunner
from mailarchive.service import ArchiveService, EventLevel, ServiceEvent
from mailarchive.storage import ArchiveState, destination_path
from mailarchive.platform_integration import (
    SingleInstance,
    activate_existing_window,
    set_start_at_login,
    tray_backend_is_available,
)


FIELD_LABELS = {
    "All emails": MailField.ALL,
    "Sender": MailField.SENDER,
    "Recipient": MailField.RECIPIENT,
    "Subject": MailField.SUBJECT,
    "Message body": MailField.BODY,
    "Has attachments": MailField.HAS_ATTACHMENT,
}
OPERATOR_LABELS = {
    "contains": MatchOperator.CONTAINS,
    "equals": MatchOperator.EQUALS,
    "starts with": MatchOperator.STARTS_WITH,
    "ends with": MatchOperator.ENDS_WITH,
}
SAVE_LABELS = {
    "Email and attachments": SaveMode.EMAIL_AND_ATTACHMENTS,
    "Email only (.eml)": SaveMode.EMAIL_ONLY,
    "Attachments only": SaveMode.ATTACHMENTS_ONLY,
}
PROVIDER_LABELS = {
    "Generic IMAP": MailProvider.GENERIC_IMAP,
    "Gmail (Google API)": MailProvider.GMAIL_API,
    "Outlook / Microsoft 365 (Microsoft Graph)": MailProvider.MICROSOFT_GRAPH,
}
AUTH_LABELS = {
    "Password": AuthMode.PASSWORD,
    "Google OAuth - user sign-in": AuthMode.OAUTH_USER,
    "Google Workspace - domain-wide delegation": AuthMode.OAUTH_APPLICATION,
    "Microsoft OAuth - delegated user access": AuthMode.OAUTH_USER,
    "Microsoft OAuth - application access": AuthMode.OAUTH_APPLICATION,
}


def _label_for(mapping: dict[str, Any], value: Any) -> str:
    return next((label for label, item in mapping.items() if item == value), str(value))


def _auth_label_for(provider: MailProvider, auth_mode: AuthMode) -> str:
    if provider == MailProvider.GMAIL_API:
        if auth_mode == AuthMode.OAUTH_APPLICATION:
            return "Google Workspace - domain-wide delegation"
        return "Google OAuth - user sign-in"
    if provider == MailProvider.MICROSOFT_GRAPH:
        if auth_mode == AuthMode.OAUTH_APPLICATION:
            return "Microsoft OAuth - application access"
        return "Microsoft OAuth - delegated user access"
    return "Password"


def _condition_summary(rule: Rule) -> str:
    if not rule.conditions or rule.conditions[0].field == MailField.ALL:
        return "All emails"
    condition = rule.conditions[0]
    field = _label_for(FIELD_LABELS, condition.field)
    if condition.field == MailField.HAS_ATTACHMENT:
        yes = condition.value.strip().casefold() not in {"", "0", "false", "no"}
        return f"{field}: {'Yes' if yes else 'No'}"
    operator = _label_for(OPERATOR_LABELS, condition.operator)
    return f'{field} {operator} "{condition.value}"'


class TrayController:
    def __init__(self, post_ui: Any, show: Any, run_now: Any, quit_app: Any) -> None:
        self.post_ui = post_ui
        self.show_callback = show
        self.run_callback = run_now
        self.quit_callback = quit_app
        self.icon: Any = None
        self.available = False
        self.safe_to_hide = False
        self._state = "ok"
        try:
            import pystray

            self.pystray = pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open MailArchive", self._show, default=True),
                pystray.MenuItem("Archive now", self._run),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )
            self.icon = pystray.Icon("MailArchive", self._image("ok"), "MailArchive - ready", menu)
            if not tray_backend_is_available(self.icon):
                self.icon = None
                return
            backend = type(self.icon).__module__.casefold()
            if os.name == "nt":
                self.icon.run_detached()
            else:
                threading.Thread(
                    target=self.icon.run,
                    name="MailArchive-Tray",
                    daemon=True,
                ).start()
            self.available = True
            self.safe_to_hide = os.name == "nt" or "appindicator" in backend or backend.endswith("._xorg")
        except Exception:
            self.available = False
            self.safe_to_hide = False

    @staticmethod
    def _image(state: str) -> Any:
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = {
            "ok": "#18794e",
            "busy": "#2563eb",
            "warning": "#b7791f",
            "error": "#c53030",
        }[state]
        draw.rounded_rectangle((5, 10, 59, 52), radius=8, fill=color)
        draw.line((8, 14, 32, 34, 56, 14), fill="white", width=5)
        return image

    def _show(self, *_: Any) -> None:
        self.post_ui(self.show_callback)

    def _run(self, *_: Any) -> None:
        self.post_ui(self.run_callback)

    def _quit(self, *_: Any) -> None:
        self.post_ui(self.quit_callback)

    def set_state(self, state: str, title: str) -> None:
        if not self.available or not self.icon:
            return
        self._state = state
        self.icon.icon = self._image(state)
        self.icon.title = title

    def notify(self, message: str) -> None:
        if self.available and self.icon:
            try:
                self.icon.notify(message, "MailArchive - problem detected")
            except Exception:
                pass

    def stop(self) -> None:
        if self.available and self.icon:
            self.icon.stop()


class AccountDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        default_poll_minutes: int,
        account: Account | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Edit email account" if account else "Add email account")
        self.resizable(False, False)
        self.result: tuple[Account, dict[str, Any]] | None = None
        self.account = account
        self.default_poll_minutes = default_poll_minutes
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=20)
        frame.grid(sticky="nsew")
        self.variables = {
            "label": tk.StringVar(value=account.label if account else ""),
            "provider": tk.StringVar(
                value=_label_for(PROVIDER_LABELS, account.provider)
                if account
                else "Generic IMAP"
            ),
            "auth": tk.StringVar(
                value=_auth_label_for(account.provider, account.auth_mode)
                if account
                else "Password"
            ),
            "host": tk.StringVar(value=account.host if account else ""),
            "port": tk.StringVar(value=str(account.port if account else 993)),
            "username": tk.StringVar(value=account.username if account else ""),
            "secret": tk.StringVar(),
            "folder": tk.StringVar(value=account.folder if account else "INBOX"),
            "client_id": tk.StringVar(value=account.client_id if account else ""),
            "tenant_id": tk.StringVar(value=account.tenant_id if account else ""),
            "service_account_file": tk.StringVar(),
            "poll": tk.StringVar(
                value=str(account.poll_minutes)
                if account and account.poll_minutes is not None
                else ""
            ),
            "ssl": tk.BooleanVar(value=account.use_ssl if account else True),
            "enabled": tk.BooleanVar(value=account.enabled if account else True),
        }
        self.widgets: dict[str, Any] = {}
        self.field_labels: dict[str, ttk.Label] = {}
        self.field_containers: dict[str, tk.Widget] = {}
        self.service_account_button: ttk.Button | None = None
        fields = [
            ("Display name", "label", "entry", None),
            ("Provider", "provider", "combo", list(PROVIDER_LABELS)),
            ("Authentication", "auth", "combo", list(AUTH_LABELS)),
            ("Mailbox email / username", "username", "entry", None),
            ("IMAP server", "host", "entry", None),
            ("IMAP port", "port", "entry", None),
            ("Folder / label", "folder", "entry", None),
            ("OAuth client ID", "client_id", "entry", None),
            ("Microsoft tenant / audience", "tenant_id", "entry", None),
            (
                "Google service-account JSON"
                + (" (leave blank to keep it)" if account else ""),
                "service_account_file",
                "file",
                None,
            ),
            (
                "Password / OAuth client secret"
                + (" (leave blank to keep it)" if account else ""),
                "secret",
                "entry",
                "*",
            ),
            (
                f"Polling override (minutes; blank uses {default_poll_minutes})",
                "poll",
                "entry",
                None,
            ),
        ]
        self.field_order = [key for _, key, _, _ in fields]
        for row, (label, key, kind, options) in enumerate(fields):
            field_label = ttk.Label(frame, text=label)
            field_label.grid(row=row, column=0, sticky="w", padx=(0, 14), pady=5)
            self.field_labels[key] = field_label
            if kind == "combo":
                widget = ttk.Combobox(
                    frame,
                    textvariable=self.variables[key],
                    values=options,
                    state="readonly",
                    width=42,
                )
                widget.grid(row=row, column=1, sticky="ew", pady=5)
                container: tk.Widget = widget
            elif kind == "file":
                file_frame = ttk.Frame(frame)
                file_frame.grid(row=row, column=1, sticky="ew", pady=5)
                widget = ttk.Entry(
                    file_frame,
                    textvariable=self.variables[key],
                    width=34,
                )
                widget.pack(side="left", fill="x", expand=True)
                self.service_account_button = ttk.Button(
                    file_frame,
                    text="Browse...",
                    command=self._choose_google_service_account_file,
                )
                self.service_account_button.pack(side="left", padx=(6, 0))
                container = file_frame
            else:
                widget = ttk.Entry(
                    frame,
                    textvariable=self.variables[key],
                    width=45,
                    show=options or "",
                )
                widget.grid(row=row, column=1, sticky="ew", pady=5)
                container = widget
            self.widgets[key] = widget
            self.field_containers[key] = container
            if row == 0:
                widget.focus_set()
        self.widgets["provider"].bind(
            "<<ComboboxSelected>>",
            lambda event: self._provider_changed(),
        )
        self.widgets["auth"].bind(
            "<<ComboboxSelected>>",
            lambda event: self._update_fields(),
        )
        row = len(fields)
        self.ssl_check = ttk.Checkbutton(
            frame,
            text="Use direct SSL/TLS (usually port 993)",
            variable=self.variables["ssl"],
        )
        self.ssl_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.enabled_check = ttk.Checkbutton(
            frame,
            text="Email account enabled",
            variable=self.variables["enabled"],
        )
        self.enabled_check.grid(row=row + 1, column=0, columnspan=2, sticky="w")
        self.help_label = ttk.Label(
            frame,
            text="",
            foreground="#555555",
            wraplength=560,
        )
        self.help_label.grid(row=row + 2, column=0, columnspan=2, sticky="w", pady=(10, 14))
        self.buttons = ttk.Frame(frame)
        self.buttons.grid(row=row + 3, column=0, columnspan=2, sticky="e")
        ttk.Button(self.buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(self.buttons, text="Save", command=self._save).pack(side="left")
        self.bind("<Return>", lambda event: self._save())
        self.bind("<Escape>", lambda event: self.destroy())
        self._update_fields()

    def _choose_google_service_account_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Select Google service-account JSON key",
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
        )
        if path:
            self.variables["service_account_file"].set(path)

    def _provider_changed(self) -> None:
        provider = PROVIDER_LABELS[self.variables["provider"].get()]
        if provider == MailProvider.GENERIC_IMAP:
            self.variables["auth"].set("Password")
            if not self.variables["folder"].get().strip():
                self.variables["folder"].set("INBOX")
        elif provider == MailProvider.GMAIL_API:
            self.variables["auth"].set("Google OAuth - user sign-in")
            if self.variables["folder"].get().strip() in {"", "inbox"}:
                self.variables["folder"].set("INBOX")
        else:
            self.variables["auth"].set("Microsoft OAuth - delegated user access")
            if self.variables["folder"].get().strip() in {"", "INBOX"}:
                self.variables["folder"].set("inbox")
        self._update_fields()

    def _layout_fields(self, visible_fields: frozenset[str], show_ssl: bool) -> None:
        row = 0
        for key in self.field_order:
            label = self.field_labels[key]
            container = self.field_containers[key]
            if key not in visible_fields:
                label.grid_remove()
                container.grid_remove()
                continue
            label.grid(row=row, column=0, sticky="w", padx=(0, 14), pady=5)
            container.grid(row=row, column=1, sticky="ew", pady=5)
            row += 1

        if show_ssl:
            self.ssl_check.grid(
                row=row,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(8, 2),
            )
            row += 1
        else:
            self.ssl_check.grid_remove()
        self.enabled_check.grid(row=row, column=0, columnspan=2, sticky="w")
        self.help_label.grid(
            row=row + 1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 14),
        )
        self.buttons.grid(row=row + 2, column=0, columnspan=2, sticky="e")

    def _update_fields(self) -> None:
        provider = PROVIDER_LABELS[self.variables["provider"].get()]
        if provider == MailProvider.GENERIC_IMAP:
            allowed_auth = ["Password"]
        elif provider == MailProvider.GMAIL_API:
            allowed_auth = [
                "Google OAuth - user sign-in",
                "Google Workspace - domain-wide delegation",
            ]
        else:
            allowed_auth = [
                "Microsoft OAuth - delegated user access",
                "Microsoft OAuth - application access",
            ]
        if self.variables["auth"].get() not in allowed_auth:
            self.variables["auth"].set(allowed_auth[0])
        auth = AUTH_LABELS[self.variables["auth"].get()]
        self.widgets["auth"].configure(values=allowed_auth)
        imap = provider == MailProvider.GENERIC_IMAP
        google = provider == MailProvider.GMAIL_API
        google_application = google and auth == AuthMode.OAUTH_APPLICATION
        visible_fields = visible_account_fields(provider, auth)

        for key, widget in self.widgets.items():
            if key in {"provider", "auth"}:
                continue
            widget.configure(state="normal" if key in visible_fields else "disabled")
        if self.service_account_button is not None:
            self.service_account_button.configure(
                state="normal" if google_application else "disabled"
            )
        self._layout_fields(visible_fields, show_ssl=imap)
        keep_suffix = " (leave blank to keep it)" if self.account else ""
        if imap:
            self.field_labels["secret"].configure(
                text="Password / app password" + keep_suffix
            )
            help_text = (
                "The password is stored in the operating system's credential store. "
                "Some IMAP providers require an app password."
            )
        elif google_application:
            help_text = (
                "For Google Workspace only. Select a service-account JSON key whose client ID "
                "has domain-wide delegation for gmail.readonly. The mailbox address is the "
                "Workspace user to impersonate; interactive authorization is not used."
            )
        elif google:
            self.field_labels["client_id"].configure(text="Google OAuth desktop client ID")
            self.field_labels["secret"].configure(
                text="Google OAuth client secret (optional)" + keep_suffix
            )
            help_text = (
                "Enter the credentials of a Google OAuth client whose application type is "
                "Desktop app. Save the account, then choose Authorize to sign in through "
                "the system browser. The client secret and token data stay in the operating "
                "system's credential store."
            )
        elif auth == AuthMode.OAUTH_APPLICATION:
            self.field_labels["client_id"].configure(
                text="Microsoft application client ID"
            )
            self.field_labels["tenant_id"].configure(text="Microsoft tenant ID")
            self.field_labels["secret"].configure(
                text="Microsoft OAuth client secret" + keep_suffix
            )
            help_text = (
                "Use a Microsoft Entra app registration with application Mail.Read permission "
                "and admin consent. Enter the tenant ID and client secret."
            )
        else:
            self.field_labels["client_id"].configure(
                text="Microsoft application client ID"
            )
            self.field_labels["tenant_id"].configure(
                text="Microsoft tenant / audience (blank uses common)"
            )
            help_text = (
                "Enter a Microsoft Entra public-client application ID. The tenant can be a "
                "directory ID, organizations, consumers, or common. Save the account, then "
                "choose Authorize to sign in through the system browser."
            )
        self.help_label.configure(text=help_text)

    def _save(self) -> None:
        try:
            provider = PROVIDER_LABELS[self.variables["provider"].get()]
            auth_mode = AUTH_LABELS[self.variables["auth"].get()]
            secret = self.variables["secret"].get()
            service_account_file = self.variables["service_account_file"].get().strip()
            google_application = (
                provider == MailProvider.GMAIL_API
                and auth_mode == AuthMode.OAUTH_APPLICATION
            )
            google_user = (
                provider == MailProvider.GMAIL_API
                and auth_mode == AuthMode.OAUTH_USER
            )
            needs_secret = (
                provider == MailProvider.GENERIC_IMAP
                or (
                    provider == MailProvider.MICROSOFT_GRAPH
                    and auth_mode == AuthMode.OAUTH_APPLICATION
                )
            )
            source_changed = self.account is None or (
                self.account.provider != provider or self.account.auth_mode != auth_mode
            )
            if needs_secret and source_changed and not secret:
                raise ValueError("Enter the password or OAuth client secret.")
            if google_application and source_changed and not service_account_file:
                raise ValueError("Select the Google service-account JSON key file.")
            poll_text = self.variables["poll"].get().strip()
            account = Account(
                id=self.account.id if self.account else Account(label="temporary").id,
                label=self.variables["label"].get().strip(),
                provider=provider,
                auth_mode=auth_mode,
                host=(
                    self.variables["host"].get().strip()
                    if provider == MailProvider.GENERIC_IMAP
                    else ""
                ),
                port=(
                    int(self.variables["port"].get())
                    if provider == MailProvider.GENERIC_IMAP
                    else 993
                ),
                username=self.variables["username"].get().strip(),
                folder=self.variables["folder"].get().strip()
                or ("inbox" if provider == MailProvider.MICROSOFT_GRAPH else "INBOX"),
                client_id=(
                    self.variables["client_id"].get().strip()
                    if (
                        auth_mode == AuthMode.OAUTH_USER
                        and provider
                        in {MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH}
                    )
                    or (
                        provider == MailProvider.MICROSOFT_GRAPH
                        and auth_mode == AuthMode.OAUTH_APPLICATION
                    )
                    else ""
                ),
                tenant_id=(
                    self.variables["tenant_id"].get().strip()
                    if provider == MailProvider.MICROSOFT_GRAPH
                    and auth_mode
                    in {AuthMode.OAUTH_USER, AuthMode.OAUTH_APPLICATION}
                    else ""
                ),
                poll_minutes=int(poll_text) if poll_text else None,
                use_ssl=(
                    bool(self.variables["ssl"].get())
                    if provider == MailProvider.GENERIC_IMAP
                    else True
                ),
                enabled=bool(self.variables["enabled"].get()),
            )
            account.validate()
            credential_updates: dict[str, Any] = {}
            if needs_secret and secret:
                credential_key = (
                    "password"
                    if provider == MailProvider.GENERIC_IMAP
                    else "client_secret"
                )
                credential_updates[credential_key] = secret
            if google_user and secret:
                credential_updates["oauth_client_secret"] = secret
            if google_application and service_account_file:
                credential_updates["google_service_account"] = (
                    parse_google_service_account_file(service_account_file)
                )
        except ValueError as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.result = (account, credential_updates)
        self.destroy()


class RuleDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, archive_root: str, rule: Rule | None = None) -> None:
        super().__init__(parent)
        self.title("Edit rule" if rule else "Add rule")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: Rule | None = None
        self.rule = rule
        condition = rule.conditions[0] if rule and rule.conditions else Condition()

        frame = ttk.Frame(self, padding=20)
        frame.grid(sticky="nsew")
        self.name_var = tk.StringVar(value=rule.name if rule else "")
        self.field_var = tk.StringVar(value=_label_for(FIELD_LABELS, condition.field))
        self.operator_var = tk.StringVar(value=_label_for(OPERATOR_LABELS, condition.operator))
        self.value_var = tk.StringVar(value=condition.value)
        self.destination_var = tk.StringVar(value=rule.destination if rule else "")
        self.save_var = tk.StringVar(
            value=_label_for(SAVE_LABELS, rule.save_mode if rule else SaveMode.EMAIL_AND_ATTACHMENTS)
        )
        self.enabled_var = tk.BooleanVar(value=rule.enabled if rule else True)
        self.archive_root = archive_root

        ttk.Label(frame, text="Rule name").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.name_var, width=42).grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Separator(frame).grid(row=1, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="When").grid(row=2, column=0, sticky="w", pady=5)
        field_box = ttk.Combobox(
            frame, textvariable=self.field_var, values=list(FIELD_LABELS), state="readonly", width=22
        )
        field_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        field_box.bind("<<ComboboxSelected>>", lambda event: self._update_fields())
        ttk.Label(frame, text="Comparison").grid(row=3, column=0, sticky="w", pady=5)
        self.operator_box = ttk.Combobox(
            frame,
            textvariable=self.operator_var,
            values=list(OPERATOR_LABELS),
            state="readonly",
            width=22,
        )
        self.operator_box.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(frame, text="Value").grid(row=4, column=0, sticky="w", pady=5)
        self.value_entry = ttk.Entry(frame, textvariable=self.value_var)
        self.value_entry.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        self.value_hint = ttk.Label(frame, text="", foreground="#555555")
        self.value_hint.grid(row=5, column=1, columnspan=2, sticky="w")
        ttk.Separator(frame).grid(row=6, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="Save to").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.destination_var).grid(row=7, column=1, sticky="ew", pady=5)
        ttk.Button(frame, text="Folder...", command=self._choose_folder).grid(row=7, column=2, padx=(6, 0))
        ttk.Label(frame, text="Save as").grid(row=8, column=0, sticky="w", pady=5)
        ttk.Combobox(
            frame, textvariable=self.save_var, values=list(SAVE_LABELS), state="readonly"
        ).grid(row=8, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Checkbutton(frame, text="Rule enabled", variable=self.enabled_var).grid(
            row=9, column=1, columnspan=2, sticky="w", pady=(8, 2)
        )
        ttk.Label(
            frame,
            text="The first matching rule in the list is used.",
            foreground="#555555",
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(8, 14))
        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=3, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Save", command=self._save).pack(side="left")
        self._update_fields()
        self.bind("<Escape>", lambda event: self.destroy())

    def _update_fields(self) -> None:
        field = FIELD_LABELS[self.field_var.get()]
        if field == MailField.ALL:
            self.operator_box.configure(state="disabled")
            self.value_entry.configure(state="disabled")
            self.value_hint.configure(text="This rule matches every email.")
        else:
            self.operator_box.configure(state="readonly")
            self.value_entry.configure(state="normal")
            if field == MailField.HAS_ATTACHMENT:
                self.operator_box.configure(state="disabled")
                self.value_hint.configure(text='Enter "Yes" or "No".')
                if not self.value_var.get():
                    self.value_var.set("Yes")
            else:
                self.value_hint.configure(text="Matching is case-insensitive.")

    def _choose_folder(self) -> None:
        Path(self.archive_root).mkdir(parents=True, exist_ok=True)
        selected = filedialog.askdirectory(parent=self, initialdir=self.archive_root)
        if not selected:
            return
        try:
            relative = Path(selected).resolve().relative_to(Path(self.archive_root).resolve())
        except ValueError:
            messagebox.showerror(
                "Invalid folder",
                "Select a folder inside the archive folder.",
                parent=self,
            )
            return
        self.destination_var.set(str(relative) if str(relative) != "." else "Inbox")

    def _save(self) -> None:
        try:
            name = self.name_var.get().strip()
            if not name:
                raise ValueError("Enter a name for the rule.")
            destination = self.destination_var.get().strip()
            destination_path(Path(self.archive_root), destination)
            field = FIELD_LABELS[self.field_var.get()]
            value = self.value_var.get().strip()
            if field not in {MailField.ALL, MailField.HAS_ATTACHMENT} and not value:
                raise ValueError("Enter a comparison value.")
            if field == MailField.HAS_ATTACHMENT and value.casefold() not in {"yes", "no", "true", "false", "1", "0"}:
                raise ValueError('For "Has attachments", enter Yes or No.')
            condition = Condition(
                field=field,
                operator=OPERATOR_LABELS[self.operator_var.get()],
                value=value,
            )
            self.result = Rule(
                id=self.rule.id if self.rule else Rule("x", "x").id,
                name=name,
                destination=destination,
                conditions=[condition],
                save_mode=SAVE_LABELS[self.save_var.get()],
                enabled=bool(self.enabled_var.get()),
            )
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.destroy()


class DesktopApp:
    def __init__(
        self,
        root: tk.Tk,
        config_store: ConfigStore,
        settings: Settings,
        credential_store: Any,
    ) -> None:
        self.root = root
        self.config_store = config_store
        self.settings = settings
        self.credential_store = credential_store
        self.ui_queue: queue.Queue[Any] = queue.Queue()
        self.state = ArchiveState(config_store.state_database_path(settings))
        self.service = ArchiveService(credential_store, self.state, self.on_service_event)
        self.runner = BackgroundRunner(self.service, lambda: self.settings)
        self._closing = False

        root.title("MailArchive")
        root.geometry("980x680")
        root.minsize(820, 580)
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._configure_style()
        self._build_ui()
        self.tray = TrayController(self.post_ui, self.show, self.run_now, self.quit)
        self.root.after(100, self._drain_ui_queue)
        try:
            set_start_at_login(self.settings.start_at_login)
        except Exception as exc:
            self.on_service_event(
                ServiceEvent(EventLevel.WARNING, f"Could not configure start at login: {exc}")
            )
        self.refresh_all()
        self.runner.start()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Sub.TLabel", foreground="#555555", font=("Segoe UI", 10))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Treeview", rowheight=28)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=(22, 18))
        container.pack(fill="both", expand=True)
        header = ttk.Frame(container)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="MailArchive", style="Header.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left", padx=(22, 0)
        )
        ttk.Button(header, text="Archive now", command=self.run_now).pack(side="right")

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self.dashboard_tab = ttk.Frame(self.notebook, padding=18)
        self.accounts_tab = ttk.Frame(self.notebook, padding=18)
        self.rules_tab = ttk.Frame(self.notebook, padding=18)
        self.settings_tab = ttk.Frame(self.notebook, padding=18)
        self.log_tab = ttk.Frame(self.notebook, padding=18)
        for tab, label in [
            (self.dashboard_tab, "Overview"),
            (self.accounts_tab, "Accounts"),
            (self.rules_tab, "Rules"),
            (self.settings_tab, "Settings"),
            (self.log_tab, "Activity log"),
        ]:
            self.notebook.add(tab, text=label)
        self._build_dashboard()
        self._build_accounts()
        self._build_rules()
        self._build_settings()
        self._build_log()

    def _build_dashboard(self) -> None:
        ttk.Label(self.dashboard_tab, text="Local email archive", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.dashboard_tab,
            text="MailArchive checks your email accounts in the background and saves matching emails according to your rules.",
            style="Sub.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(4, 20))
        summary = ttk.Frame(self.dashboard_tab)
        summary.pack(fill="x")
        self.account_summary = tk.StringVar()
        self.rule_summary = tk.StringVar()
        self.archive_summary = tk.StringVar()
        for column, (title, variable) in enumerate(
            [
                ("Active email accounts", self.account_summary),
                ("Active rules", self.rule_summary),
                ("Archive folder", self.archive_summary),
            ]
        ):
            card = ttk.LabelFrame(summary, text=title, padding=14)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            ttk.Label(card, textvariable=variable, font=("Segoe UI", 12, "bold"), wraplength=260).pack(anchor="w")
            summary.columnconfigure(column, weight=1)
        actions = ttk.Frame(self.dashboard_tab)
        actions.pack(fill="x", pady=24)
        ttk.Button(actions, text="Add email account", command=self.add_account).pack(side="left")
        ttk.Button(actions, text="Add rule", command=self.add_rule).pack(side="left", padx=8)
        ttk.Button(actions, text="Open archive folder", command=self.open_archive).pack(side="left")
        ttk.Label(
            self.dashboard_tab,
            text="Note: Emails on the server are never deleted, moved, or marked as read.",
            foreground="#18794e",
        ).pack(anchor="w", pady=(10, 0))

    def _build_accounts(self) -> None:
        ttk.Label(self.accounts_tab, text="Email accounts", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.accounts_tab,
            text="Use generic IMAP, the Gmail API, or Microsoft Graph for Outlook and Microsoft 365.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        columns = ("name", "provider", "user", "interval", "active")
        self.account_tree = ttk.Treeview(self.accounts_tab, columns=columns, show="headings", selectmode="browse")
        for key, title, width in [
            ("name", "Name", 150),
            ("provider", "Provider", 230),
            ("user", "User", 220),
            ("interval", "Polling", 100),
            ("active", "Status", 90),
        ]:
            self.account_tree.heading(key, text=title)
            self.account_tree.column(key, width=width, anchor="w")
        self.account_tree.pack(fill="both", expand=True)
        self.account_tree.bind("<Double-1>", lambda event: self.edit_account())
        buttons = ttk.Frame(self.accounts_tab)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Add", command=self.add_account).pack(side="left")
        ttk.Button(buttons, text="Edit", command=self.edit_account).pack(side="left", padx=6)
        ttk.Button(buttons, text="Remove", command=self.remove_account).pack(side="left")
        ttk.Button(
            buttons,
            text="Authorize",
            command=self.authorize_selected_account,
        ).pack(side="left", padx=(16, 0))

    def _build_rules(self) -> None:
        ttk.Label(self.rules_tab, text="Archive rules", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.rules_tab,
            text="Rules are evaluated from top to bottom. The first match determines the destination and save mode.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        columns = ("order", "name", "condition", "destination", "mode", "active")
        self.rule_tree = ttk.Treeview(self.rules_tab, columns=columns, show="headings", selectmode="browse")
        for key, title, width in [
            ("order", "#", 40),
            ("name", "Name", 160),
            ("condition", "When", 260),
            ("destination", "Destination", 140),
            ("mode", "Save as", 150),
            ("active", "Status", 70),
        ]:
            self.rule_tree.heading(key, text=title)
            self.rule_tree.column(key, width=width, anchor="w")
        self.rule_tree.pack(fill="both", expand=True)
        self.rule_tree.bind("<Double-1>", lambda event: self.edit_rule())
        buttons = ttk.Frame(self.rules_tab)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Add", command=self.add_rule).pack(side="left")
        ttk.Button(buttons, text="Edit", command=self.edit_rule).pack(side="left", padx=6)
        ttk.Button(buttons, text="Remove", command=self.remove_rule).pack(side="left")
        ttk.Button(buttons, text="Move up", command=lambda: self.move_rule(-1)).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Move down", command=lambda: self.move_rule(1)).pack(side="right")

    def _build_settings(self) -> None:
        ttk.Label(self.settings_tab, text="Settings", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        settings_pages = ttk.Notebook(self.settings_tab)
        settings_pages.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(18, 0))
        general_page = ttk.Frame(settings_pages, padding=16)
        advanced_page = ttk.Frame(settings_pages, padding=16)
        settings_pages.add(general_page, text="General")
        settings_pages.add(advanced_page, text="Advanced")

        ttk.Label(general_page, text="Archive folder").grid(row=0, column=0, sticky="w")
        self.archive_var = tk.StringVar(value=self.settings.archive_root)
        ttk.Entry(general_page, textvariable=self.archive_var).grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(6, 0)
        )
        ttk.Button(general_page, text="Choose...", command=self.choose_archive).grid(
            row=1, column=2, pady=(6, 0)
        )
        ttk.Label(general_page, text="Default polling interval (minutes)").grid(
            row=2,
            column=0,
            sticky="w",
            pady=(22, 6),
        )
        self.poll_var = tk.StringVar(value=str(self.settings.default_poll_minutes))
        ttk.Entry(general_page, textvariable=self.poll_var, width=12).grid(
            row=3,
            column=0,
            sticky="w",
        )
        ttk.Label(
            general_page,
            text="Used by every account without its own polling override.",
            style="Sub.TLabel",
        ).grid(row=3, column=1, columnspan=2, sticky="w")
        self.startup_var = tk.BooleanVar(value=self.settings.start_at_login)
        self.minimize_var = tk.BooleanVar(value=self.settings.minimize_to_tray)
        self.warning_var = tk.BooleanVar(value=self.settings.warn_on_error)
        ttk.Checkbutton(
            general_page, text="Start automatically at login", variable=self.startup_var
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(22, 6))
        ttk.Checkbutton(
            general_page,
            text="Keep running in the notification area when closed",
            variable=self.minimize_var,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Checkbutton(
            general_page,
            text="Show a desktop notification when an error occurs",
            variable=self.warning_var,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=6)

        ttk.Label(advanced_page, text="SQLite database file").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.database_var = tk.StringVar(
            value=str(self.config_store.state_database_path(self.settings))
        )
        ttk.Entry(advanced_page, textvariable=self.database_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0)
        )
        ttk.Button(
            advanced_page,
            text="Choose...",
            command=self.choose_state_database,
        ).grid(row=1, column=1, pady=(6, 0))
        ttk.Button(
            advanced_page,
            text="Use default",
            command=self.use_default_state_database,
        ).grid(row=1, column=2, padx=(8, 0), pady=(6, 0))
        ttk.Label(
            advanced_page,
            text=(
                "This database tracks which messages were already archived. When the path "
                "changes, the existing processing history is copied into the selected database."
            ),
            style="Sub.TLabel",
            wraplength=720,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        ttk.Button(self.settings_tab, text="Save settings", command=self.save_settings).grid(
            row=2, column=0, sticky="w", pady=(18, 0)
        )
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.columnconfigure(1, weight=1)
        self.settings_tab.rowconfigure(1, weight=1)
        general_page.columnconfigure(0, weight=1)
        general_page.columnconfigure(1, weight=1)
        advanced_page.columnconfigure(0, weight=1)

    def _build_log(self) -> None:
        ttk.Label(self.log_tab, text="Activity log", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.log_tab,
            text="Successful checks and clear error messages appear here.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        columns = ("time", "level", "message")
        self.log_tree = ttk.Treeview(self.log_tab, columns=columns, show="headings")
        for key, title, width in [
            ("time", "Time", 130),
            ("level", "Status", 90),
            ("message", "Message", 650),
        ]:
            self.log_tree.heading(key, text=title)
            self.log_tree.column(key, width=width, anchor="w")
        self.log_tree.pack(fill="both", expand=True)

    def refresh_all(self) -> None:
        self.account_tree.delete(*self.account_tree.get_children())
        for account in self.settings.accounts:
            self.account_tree.insert(
                "",
                "end",
                iid=account.id,
                values=(
                    account.label,
                    _label_for(PROVIDER_LABELS, account.provider),
                    account.username,
                    f"{account.poll_minutes or self.settings.default_poll_minutes} min"
                    + (" (default)" if account.poll_minutes is None else ""),
                    "Active" if account.enabled else "Paused",
                ),
            )
        self.rule_tree.delete(*self.rule_tree.get_children())
        for index, rule in enumerate(self.settings.rules, start=1):
            self.rule_tree.insert(
                "",
                "end",
                iid=rule.id,
                values=(
                    index,
                    rule.name,
                    _condition_summary(rule),
                    rule.destination,
                    _label_for(SAVE_LABELS, rule.save_mode),
                    "Active" if rule.enabled else "Off",
                ),
            )
        self.account_summary.set(str(sum(account.enabled for account in self.settings.accounts)))
        self.rule_summary.set(str(sum(rule.enabled for rule in self.settings.rules)))
        self.archive_summary.set(self.settings.archive_root)

    def _selected_account(self) -> Account | None:
        selected = self.account_tree.selection()
        return next((item for item in self.settings.accounts if selected and item.id == selected[0]), None)

    def add_account(self) -> None:
        dialog = AccountDialog(self.root, self.settings.default_poll_minutes)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        account, credential_updates = dialog.result
        try:
            self._store_account_credentials(account, credential_updates)
            self.settings.accounts.append(account)
            self._persist()
        except Exception as exc:
            messagebox.showerror("Email account not saved", str(exc), parent=self.root)

    def edit_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo("Select an account", "Select an email account first.")
            return
        dialog = AccountDialog(self.root, self.settings.default_poll_minutes, account)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        replacement, credential_updates = dialog.result
        try:
            replace_credentials = (
                account.provider != replacement.provider
                or account.auth_mode != replacement.auth_mode
                or account.client_id != replacement.client_id
                or account.tenant_id != replacement.tenant_id
                or "oauth_client_secret" in credential_updates
                or "client_secret" in credential_updates
            )
            self._store_account_credentials(
                replacement,
                credential_updates,
                replace=replace_credentials,
            )
            index = self.settings.accounts.index(account)
            self.settings.accounts[index] = replacement
            self._persist()
        except Exception as exc:
            messagebox.showerror("Email account not saved", str(exc), parent=self.root)

    def _store_account_credentials(
        self,
        account: Account,
        updates: dict[str, Any],
        replace: bool = False,
    ) -> None:
        if account.provider == MailProvider.GENERIC_IMAP:
            allowed_keys = {"password"}
        elif account.provider == MailProvider.GMAIL_API:
            allowed_keys = (
                {"google_service_account"}
                if account.auth_mode == AuthMode.OAUTH_APPLICATION
                else {"google_credentials", "oauth_client_secret"}
            )
        elif account.auth_mode == AuthMode.OAUTH_APPLICATION:
            allowed_keys = {"client_secret", "msal_cache"}
        else:
            allowed_keys = {"msal_cache"}

        existing = {} if replace else load_credential_data(
            self.credential_store,
            account.id,
        )
        credentials = {
            key: value
            for key, value in existing.items()
            if key in allowed_keys
        }
        credentials.update(
            {
                key: value
                for key, value in updates.items()
                if key in allowed_keys
            }
        )
        if credentials:
            save_credential_data(self.credential_store, account.id, credentials)
        elif existing or replace:
            self.credential_store.delete(account.id)

    def authorize_selected_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo("Select an account", "Select an email account first.")
            return
        if account.provider == MailProvider.GENERIC_IMAP:
            messagebox.showinfo(
                "Authorization not required",
                "This account uses its stored IMAP password and does not have an interactive OAuth sign-in.",
                parent=self.root,
            )
            return
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            if account.provider == MailProvider.GMAIL_API:
                detail = (
                    "Google Workspace application access uses the saved service-account key "
                    "and domain-wide delegation automatically. Choose Archive now to test access."
                )
            else:
                detail = (
                    "Microsoft application access uses the saved tenant ID, client ID, and "
                    "client secret automatically. Choose Archive now to test access."
                )
            messagebox.showinfo(
                "Application access",
                detail,
                parent=self.root,
            )
            return
        self.status_var.set(f"{account.label}: Waiting for authorization...")
        self.tray.set_state("busy", "MailArchive - authorization in progress")

        def authorize() -> None:
            try:
                authorize_account(account, self.credential_store)
            except Exception as exc:
                self.on_service_event(
                    ServiceEvent(
                        EventLevel.ERROR,
                        f"{account.label}: Authorization failed: {exc}",
                        account.id,
                    )
                )
            else:
                self.on_service_event(
                    ServiceEvent(
                        EventLevel.SUCCESS,
                        f"{account.label}: Authorization completed.",
                        account.id,
                    )
                )

        threading.Thread(
            target=authorize,
            name=f"MailArchive-Authorize-{account.id}",
            daemon=True,
        ).start()

    def remove_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo("Select an account", "Select an email account first.")
            return
        if not messagebox.askyesno(
            "Remove email account",
            f'Remove "{account.label}" from MailArchive?\n\nFiles already archived will be kept.',
        ):
            return
        index = self.settings.accounts.index(account)
        self.settings.accounts.remove(account)
        try:
            self._persist()
        except Exception as exc:
            self.settings.accounts.insert(index, account)
            self.refresh_all()
            messagebox.showerror("Mailbox not removed", str(exc), parent=self.root)
            return
        try:
            self.credential_store.delete(account.id)
        except Exception as exc:
            messagebox.showwarning(
                "Email account removed",
                "The email account was removed, but its stored credentials could not be "
                f"deleted: {exc}",
                parent=self.root,
            )

    def _selected_rule(self) -> Rule | None:
        selected = self.rule_tree.selection()
        return next((item for item in self.settings.rules if selected and item.id == selected[0]), None)

    def add_rule(self) -> None:
        dialog = RuleDialog(self.root, self.settings.archive_root)
        self.root.wait_window(dialog)
        if dialog.result:
            catch_all_index = next(
                (
                    index
                    for index, rule in enumerate(self.settings.rules)
                    if rule.conditions and rule.conditions[0].field == MailField.ALL
                ),
                len(self.settings.rules),
            )
            self.settings.rules.insert(catch_all_index, dialog.result)
            self._persist()

    def edit_rule(self) -> None:
        rule = self._selected_rule()
        if not rule:
            messagebox.showinfo("Select a rule", "Select a rule first.")
            return
        dialog = RuleDialog(self.root, self.settings.archive_root, rule)
        self.root.wait_window(dialog)
        if dialog.result:
            index = self.settings.rules.index(rule)
            self.settings.rules[index] = dialog.result
            self._persist()

    def remove_rule(self) -> None:
        rule = self._selected_rule()
        if not rule:
            messagebox.showinfo("Select a rule", "Select a rule first.")
            return
        if len(self.settings.rules) == 1:
            messagebox.showerror("Rule required", "At least one archive rule must remain.")
            return
        if messagebox.askyesno("Remove rule", f'Remove the rule "{rule.name}"?'):
            self.settings.rules.remove(rule)
            self._persist()

    def move_rule(self, offset: int) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        index = self.settings.rules.index(rule)
        target = index + offset
        if not 0 <= target < len(self.settings.rules):
            return
        self.settings.rules[index], self.settings.rules[target] = (
            self.settings.rules[target],
            self.settings.rules[index],
        )
        self._persist()
        self.rule_tree.selection_set(rule.id)

    def choose_archive(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, initialdir=self.archive_var.get())
        if selected:
            self.archive_var.set(selected)

    def choose_state_database(self) -> None:
        current = Path(self.database_var.get()).expanduser()
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Choose SQLite database",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".sqlite3",
            filetypes=[
                ("SQLite database", "*.sqlite3 *.sqlite *.db"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            self.database_var.set(selected)

    def use_default_state_database(self) -> None:
        self.database_var.set(str(self.config_store.default_state_database_path))

    def save_settings(self) -> None:
        try:
            archive_root = Path(self.archive_var.get()).expanduser()
            database_value = self.database_var.get().strip()
            if not database_value:
                raise ValueError("Choose a file for the SQLite database.")
            database_path = Path(database_value).expanduser()
            if database_path.exists() and database_path.is_dir():
                raise ValueError("The SQLite database path must point to a file, not a folder.")
            database_path = database_path.resolve()
            default_poll_minutes = int(self.poll_var.get())
            if not 1 <= default_poll_minutes <= 1440:
                raise ValueError(
                    "The default polling interval must be between 1 and 1440 minutes."
                )
            archive_root.mkdir(parents=True, exist_ok=True)
            default_database_path = self.config_store.default_state_database_path.resolve()
            candidate = replace(
                self.settings,
                archive_root=str(archive_root.resolve()),
                default_poll_minutes=default_poll_minutes,
                start_at_login=bool(self.startup_var.get()),
                minimize_to_tray=bool(self.minimize_var.get()),
                warn_on_error=bool(self.warning_var.get()),
                state_database_path=(
                    "" if database_path == default_database_path else str(database_path)
                ),
            )
            candidate.validate()

            previous_database_path = self.state.database_path.expanduser().resolve()
            database_changed = database_path != previous_database_path
            startup_changed = candidate.start_at_login != self.settings.start_at_login
            try:
                if database_changed:
                    self.state = self.service.relocate_state_database(database_path)
                set_start_at_login(candidate.start_at_login)
                self.config_store.save(candidate)
            except Exception:
                if startup_changed:
                    try:
                        set_start_at_login(self.settings.start_at_login)
                    except Exception:
                        pass
                if database_changed:
                    try:
                        self.state = self.service.relocate_state_database(
                            previous_database_path
                        )
                    except Exception:
                        pass
                raise

            self.settings = candidate
            self.database_var.set(str(database_path))
            self.refresh_all()
            messagebox.showinfo("Saved", "The settings have been saved.")
        except Exception as exc:
            messagebox.showerror("Settings not saved", str(exc))

    def _persist(self) -> None:
        self.config_store.save(self.settings)
        self.refresh_all()

    def run_now(self) -> None:
        self.status_var.set("Starting archive run...")
        self.tray.set_state("busy", "MailArchive - checking mail")
        self.runner.run_now()

    def on_service_event(self, event: ServiceEvent) -> None:
        self.post_ui(lambda: self._display_event(event))

    def post_ui(self, callback: Any) -> None:
        if not self._closing:
            self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()
        if not self._closing:
            self.root.after(100, self._drain_ui_queue)

    def _display_event(self, event: ServiceEvent) -> None:
        labels = {
            EventLevel.INFO: "Info",
            EventLevel.SUCCESS: "Success",
            EventLevel.WARNING: "Warning",
            EventLevel.ERROR: "Error",
        }
        self.status_var.set(event.message)
        self.log_tree.insert(
            "",
            0,
            values=(event.created_at.strftime("%Y-%m-%d %H:%M:%S"), labels[event.level], event.message),
        )
        children = self.log_tree.get_children()
        if len(children) > 300:
            self.log_tree.delete(*children[300:])
        if event.level == EventLevel.ERROR:
            self.tray.set_state("error", "MailArchive - problem detected")
            if self.settings.warn_on_error:
                self.tray.notify(event.message)
        elif event.level == EventLevel.WARNING:
            self.tray.set_state("warning", "MailArchive - attention required")
            if self.settings.warn_on_error:
                self.tray.notify(event.message)
        elif event.level == EventLevel.SUCCESS:
            self.tray.set_state("ok", "MailArchive - ready")

    def open_archive(self) -> None:
        try:
            path = Path(self.settings.archive_root)
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Could not open folder", str(exc))

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self) -> None:
        if self.settings.minimize_to_tray and self.tray.safe_to_hide:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.runner.stop()
        self.tray.stop()
        self.root.destroy()


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MailArchive")
    parser.add_argument("--minimized", action="store_true", help="Start in the notification area")
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    instance = SingleInstance()
    if instance.already_running:
        activate_existing_window()
        instance.close()
        return
    root = tk.Tk()
    try:
        config_store = ConfigStore()
        try:
            settings = config_store.load()
        except RuntimeError as exc:
            messagebox.showerror("MailArchive", str(exc))
            settings = Settings.defaults()
        credential_store: Any
        credential_warning: str | None = None
        if os.name == "nt":
            credential_store = WindowsCredentialStore()
        else:
            try:
                credential_store = KeyringCredentialStore()
            except Exception as exc:
                credential_warning = str(exc)
                credential_store = UnavailableCredentialStore(credential_warning)
        app = DesktopApp(root, config_store, settings, credential_store)
        if credential_warning:
            app.on_service_event(
                ServiceEvent(
                    EventLevel.ERROR,
                    "Secure credential storage is unavailable: " + credential_warning,
                )
            )
        if arguments.minimized and app.tray.safe_to_hide:
            root.withdraw()
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
