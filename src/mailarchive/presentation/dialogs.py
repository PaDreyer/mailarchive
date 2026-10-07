from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from mailarchive.application.account_edit import AccountEditSession
from mailarchive.application.account_status import (
    AccountAction,
    AuthorizationOutcome,
    AuthorizationState,
    AuthorizationStatus,
    account_status,
)
from mailarchive.domain.archive_paths import destination_path
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Condition,
    Mailbox,
    MailField,
    MailProvider,
    MatchMode,
    Rule,
    RuleTarget,
    SaveMode,
)
from mailarchive.domain.time_ranges import local_days_to_utc
from mailarchive.presentation.account_form import (
    AccountFormValues,
    AccountSubmission,
    build_account_configuration,
    build_account_submission,
    visible_account_fields,
)
from mailarchive.presentation.condition_editor import ConditionsEditor
from mailarchive.presentation.folder_picker import choose_destination_folder
from mailarchive.presentation.rule_form import (
    DestinationValidationError,
    RuleFormValues,
    build_rule,
    has_simple_matching,
    rule_account_options,
)
from mailarchive.presentation.scrollable_frame import ScrollableFrame
from mailarchive.presentation.timezone_choices import timezone_choices
from mailarchive.presentation.ui_text import (
    AUTH_LABELS,
    AUTHORIZATION_STATE_LABELS,
    FIELD_LABELS,
    OPERATOR_LABELS,
    PROVIDER_LABELS,
    SAVE_LABELS,
    _auth_label_for,
    _label_for,
)


@dataclass(frozen=True, slots=True)
class RangeSelection:
    start: datetime | None
    end: datetime | None
    timezone_name: str


class RangeDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, rule: Rule, timezone_name: str) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Apply rule to past mail")
        self.transient(parent)
        self.resizable(True, False)
        self.result: RangeSelection | None = None
        self.rule = rule
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Received from (YYYY-MM-DD; blank = earliest)").grid(
            row=0, column=0, sticky="w"
        )
        self.start_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.start_var).grid(row=1, column=0, sticky="ew")
        ttk.Label(frame, text="Through (YYYY-MM-DD, inclusive; blank = latest)").grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        self.end_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.end_var).grid(row=3, column=0, sticky="ew")
        ttk.Label(frame, text="Timezone for those days").grid(
            row=4, column=0, sticky="w", pady=(10, 0)
        )
        self.zone_var = tk.StringVar(value=timezone_name)
        self.zone_box = ttk.Combobox(
            frame,
            textvariable=self.zone_var,
            values=timezone_choices(timezone_name),
            state="readonly",
        )
        self.zone_box.grid(row=5, column=0, sticky="ew")
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Start", command=self._save).pack(side="left")
        frame.columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _event: self.destroy())
        _center_on_parent(self, parent)
        self.deiconify()
        self.grab_set()

    def _save(self) -> None:
        try:
            start_day = (
                date.fromisoformat(self.start_var.get().strip())
                if self.start_var.get().strip()
                else None
            )
            end_day = (
                date.fromisoformat(self.end_var.get().strip())
                if self.end_var.get().strip()
                else None
            )
            start, end = local_days_to_utc(start_day, end_day, self.zone_var.get().strip())
            summary = (
                f"Rule: {self.rule.name}\n"
                f"Timezone: {self.zone_var.get().strip()}\n"
                f"UTC range: {start or 'earliest'} through {end or 'latest'} (exclusive)\n"
                "Only the selected rule will be applied. Existing archive files stay in place; "
                "new matching outputs are added."
            )
            if not messagebox.askyesno("Apply rule to past mail?", summary, parent=self):
                return
            self.result = RangeSelection(start, end, self.zone_var.get().strip())
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.destroy()


_ACCOUNT_DIALOG_LAYOUTS = (
    ("Generic IMAP", "Password"),
    ("Generic IMAP", "Microsoft OAuth (XOAUTH2)"),
    ("Gmail (Google API)", "Google OAuth - user sign-in"),
    ("Gmail (Google API)", "Google Workspace - domain-wide delegation"),
    ("Outlook / Microsoft 365 (Microsoft Graph)", "Microsoft OAuth - delegated user access"),
    ("Outlook / Microsoft 365 (Microsoft Graph)", "Microsoft OAuth - application access"),
)


def _wrap_label_to_width(label: ttk.Label) -> None:
    label.configure(width=1)
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(1, event.width)))


def _center_on_parent(
    dialog: tk.Toplevel,
    parent: tk.Misc,
    *,
    width: int | None = None,
    height: int | None = None,
    keep_visible: bool = False,
) -> None:
    """Position a hidden dialog over its parent using desktop coordinates."""
    parent.update_idletasks()
    dialog.update_idletasks()
    width = dialog.winfo_reqwidth() if width is None else width
    height = dialog.winfo_reqheight() if height is None else height
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    if keep_visible:
        left, top = dialog.winfo_vrootx(), dialog.winfo_vrooty()
        x = max(left, min(x, left + dialog.winfo_vrootwidth() - width))
        y = max(top + 24, min(y, top + dialog.winfo_vrootheight() - height - 56))
    # The leading '+' makes even negative coordinates relative to the desktop
    # origin; '-x' on its own would anchor to the screen's right/bottom edge.
    dialog.geometry(f"{width}x{height}+{x}+{y}")


class MailboxDialog(tk.Toplevel):
    def __init__(
        self, parent: tk.Misc, *, mailbox: Mailbox | None = None, address: str = ""
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Edit mailbox" if mailbox else "Add mailbox")
        self.transient(parent)
        self.resizable(True, True)
        self.result: Mailbox | None = None
        self.original_mailbox = mailbox
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        self.address = tk.StringVar(value=mailbox.address if mailbox else address)
        ttk.Label(frame, text="Mailbox address / username").pack(anchor="w")
        entry = ttk.Entry(frame, textvariable=self.address, width=55)
        entry.pack(fill="x", pady=(4, 10))
        ttk.Label(frame, text="Folders / label IDs: one per line; blank reads all folders").pack(
            anchor="w"
        )
        folder_frame = ttk.Frame(frame)
        folder_frame.pack(fill="both", expand=True, pady=(4, 10))
        self.folders = tk.Text(folder_frame, width=55, height=7, wrap="none")
        folder_scroll = ttk.Scrollbar(folder_frame, orient="vertical", command=self.folders.yview)
        self.folders.configure(yscrollcommand=folder_scroll.set)
        self.folders.pack(side="left", fill="both", expand=True)
        folder_scroll.pack(side="right", fill="y")
        if mailbox:
            self.folders.insert("1.0", "\n".join(mailbox.folders))
        self.existing = tk.BooleanVar(value=mailbox.archive_existing_messages if mailbox else False)
        self.enabled = tk.BooleanVar(value=mailbox.enabled if mailbox else True)
        ttk.Checkbutton(
            frame,
            text="Archive messages already present on the first check",
            variable=self.existing,
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "This choice applies to mailboxes and folders not checked yet. "
                "On the Rules tab, use Apply to past mail to revisit earlier checks."
            ),
            wraplength=480,
        ).pack(anchor="w", pady=(4, 8))
        ttk.Checkbutton(frame, text="Mailbox enabled", variable=self.enabled).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="Save", command=self._save).pack(side="left")
        self.bind("<Escape>", lambda event: self.destroy())
        _center_on_parent(self, parent)
        self.deiconify()
        self.update_idletasks()
        self.grab_set()
        entry.focus_set()

    def _save(self) -> None:
        try:
            mailbox = Mailbox(
                self.address.get().strip(),
                folders=[
                    line for line in self.folders.get("1.0", "end").splitlines() if line.strip()
                ],
                archive_existing_messages=self.existing.get(),
                enabled=self.enabled.get(),
                **(
                    {"id": self.original_mailbox.id}
                    if self.original_mailbox
                    and self.original_mailbox.address.casefold()
                    == self.address.get().strip().casefold()
                    else {}
                ),
            )
            mailbox.validate()
        except ValueError as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.result = mailbox
        self.destroy()


class AccountDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        default_poll_minutes: int,
        account: Account | None = None,
        *,
        read_service_account: Callable[[str], dict] | None = None,
        editor: AccountEditSession | None = None,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Edit email account" if account else "Add email account")
        self.resizable(False, False)
        self.result: AccountSubmission | None = None
        self.account = account
        self.mailboxes = deepcopy(account.mailboxes) if account else []
        self.default_poll_minutes = default_poll_minutes
        self.read_service_account = read_service_account
        self.editor = editor
        self._account_id = account.id if account else str(uuid4())
        self._authorization_error = ""
        self._authorization_timer = None
        self._variable_traces: list[tuple[tk.Variable, str]] = []
        self._authorization_submission: AccountSubmission | None = None
        self.transient(parent)

        self.dialog_frame = ttk.Frame(self, padding=20)
        self.dialog_frame.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.dialog_frame.columnconfigure(0, weight=1)
        self.dialog_frame.rowconfigure(0, weight=1)
        self.form_scroll = ScrollableFrame(self.dialog_frame)
        self.form_scroll.grid(row=0, column=0, sticky="nsew")
        frame = self.form_scroll.content
        frame.columnconfigure(1, weight=1)
        self.variables = {
            "label": tk.StringVar(value=account.label if account else ""),
            "provider": tk.StringVar(
                value=_label_for(PROVIDER_LABELS, account.provider) if account else "Generic IMAP"
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
        self.widgets: dict[str, ttk.Widget] = {}
        self.field_labels: dict[str, ttk.Label] = {}
        self.field_containers: dict[str, tk.Widget] = {}
        self.service_account_button: ttk.Button | None = None
        fields = [
            ("Display name", "label", "entry", None),
            ("Provider", "provider", "combo", list(PROVIDER_LABELS)),
            ("Authentication", "auth", "combo", list(AUTH_LABELS)),
            ("Sign-in email / username", "username", "entry", None),
            ("IMAP server", "host", "entry", None),
            ("IMAP port", "port", "entry", None),
            ("OAuth client ID", "client_id", "entry", None),
            ("Microsoft tenant / audience", "tenant_id", "entry", None),
            (
                "Google service-account JSON" + (" (leave blank to keep it)" if account else ""),
                "service_account_file",
                "file",
                None,
            ),
            (
                "Password / OAuth client secret" + (" (leave blank to keep it)" if account else ""),
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
                    text="Browse",
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
        self.mailboxes_frame = ttk.LabelFrame(frame, text="Mailboxes", padding=8)
        self.mailboxes_frame.grid(row=row + 2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        mailbox_list = ttk.Frame(self.mailboxes_frame)
        mailbox_list.pack(fill="x")
        self.mailboxes_tree = ttk.Treeview(
            mailbox_list,
            columns=("address", "folders", "existing", "enabled"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        for key, title, width in (
            ("address", "Address", 190),
            ("folders", "Folders / labels", 180),
            ("existing", "Existing mail", 100),
            ("enabled", "Enabled", 60),
        ):
            self.mailboxes_tree.heading(key, text=title)
            self.mailboxes_tree.column(key, width=width)
        scrollbar = ttk.Scrollbar(
            mailbox_list, orient="vertical", command=self.mailboxes_tree.yview
        )
        self.mailboxes_tree.configure(yscrollcommand=scrollbar.set)
        self.mailboxes_tree.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")
        actions = ttk.Frame(self.mailboxes_frame)
        actions.pack(fill="x", pady=(6, 0))
        for label, action in (
            ("Add mailbox", self._add_mailbox),
            ("Edit mailbox", self._edit_mailbox),
            ("Remove", self._remove_mailbox),
        ):
            ttk.Button(actions, text=label, command=action).pack(side="left", padx=(0, 6))
        self.mailboxes_tree.bind("<Double-1>", lambda event: self._edit_mailbox())
        self._refresh_mailboxes()
        self.help_label = ttk.Label(
            frame,
            text="",
            foreground="#555555",
            wraplength=560,
        )
        self.help_label.grid(row=row + 3, column=0, columnspan=2, sticky="w", pady=(10, 14))
        self.authorization_frame = ttk.LabelFrame(frame, text="Authorization", padding=10)
        self.authorization_frame.grid(
            row=row + 4, column=0, columnspan=2, sticky="ew", pady=(0, 14)
        )
        self.authorization_label = ttk.Label(self.authorization_frame)
        self.authorization_label.pack(anchor="w")
        self.authorization_help = ttk.Label(
            self.authorization_frame,
            text="Authorize opens your browser using these inputs and keeps this dialog open.\n"
            "Click Save to keep the account and authorization.",
            wraplength=560,
        )
        self.authorization_help.pack(anchor="w", pady=(4, 8))
        self.authorization_detail = ttk.Label(self.authorization_frame, wraplength=560)
        self.authorization_detail.pack(anchor="w", pady=(0, 8))
        authorization_actions = ttk.Frame(self.authorization_frame)
        authorization_actions.pack(anchor="w")
        self.authorize_button = ttk.Button(
            authorization_actions, text="Authorize", command=self._authorize
        )
        self.authorize_button.pack(side="left")
        self.cancel_authorization_button = ttk.Button(
            authorization_actions,
            text="Cancel authorization",
            command=self._cancel_authorization,
            state="disabled",
        )
        self.cancel_authorization_button.pack(side="left", padx=(6, 0))
        self.retry_credentials_button = ttk.Button(
            authorization_actions,
            text="Retry credential check",
            command=self._retry_credentials,
        )
        self.buttons = ttk.Frame(self.dialog_frame)
        self.buttons.grid(row=1, column=0, sticky="e", pady=(12, 0))
        ttk.Button(self.buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        self.save_button = ttk.Button(self.buttons, text="Save", command=self._save)
        self.save_button.pack(side="left")
        self.bind("<Return>", lambda event: self._save())
        self.bind("<Escape>", lambda event: self.destroy())
        for key in ("username", "client_id", "tenant_id", "secret", "enabled"):
            variable = self.variables[key]
            handle = variable.trace_add("write", lambda *_: self._update_authorization())
            self._variable_traces.append((variable, handle))
        width, height = self._fix_size_for_layouts()
        self.form_scroll.bind_widgets()
        _center_on_parent(self, parent, width=width, height=height, keep_visible=True)
        self.deiconify()
        self.update_idletasks()
        self.grab_set()
        self.widgets["label"].focus_set()
        if self.editor is not None:
            self._authorization_timer = self.after(250, self._poll_authorization)

    def _poll_authorization(self) -> None:
        self._authorization_timer = None
        self._update_authorization()
        self._authorization_timer = self.after(250, self._poll_authorization)

    def destroy(self) -> None:
        if self.editor is not None:
            self.editor.close()
        if self._authorization_timer is not None:
            self.after_cancel(self._authorization_timer)
            self._authorization_timer = None
        for variable, handle in self._variable_traces:
            variable.trace_remove("write", handle)
        self._variable_traces.clear()
        super().destroy()

    def _fix_size_for_layouts(self) -> tuple[int, int]:
        original_provider = self.variables["provider"].get()
        original_auth = self.variables["auth"].get()
        width = 0
        height = 0
        for provider, auth in _ACCOUNT_DIALOG_LAYOUTS:
            self.variables["provider"].set(provider)
            self.variables["auth"].set(auth)
            self._update_fields()
            self.update_idletasks()
            width = max(width, self.form_scroll.content.winfo_reqwidth())
            height = max(height, self.form_scroll.content.winfo_reqheight())
        self.variables["provider"].set(original_provider)
        self.variables["auth"].set(original_auth)
        self._update_fields()
        width = min(width + 58, self.winfo_screenwidth() - 48)
        for label in self.field_labels.values():
            label.configure(wraplength=max(120, (width - 58) * 0.45))
        self.form_scroll.canvas.configure(width=width - 58)
        self.update_idletasks()
        footer_height = 52 + self.buttons.winfo_reqheight()
        height = min(height + footer_height, self.winfo_screenheight() - 80)
        self.form_scroll.canvas.configure(height=max(1, height - footer_height))
        self.minsize(width, height)
        self.geometry(f"{width}x{height}")
        return width, height

    def _choose_google_service_account_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Select Google service-account JSON key",
            filetypes=[("JSON files", "*.json"), ("All files", "*")],
        )
        if path:
            self.variables["service_account_file"].set(path)

    def _refresh_mailboxes(self) -> None:
        self.mailboxes_tree.delete(*self.mailboxes_tree.get_children())
        for index, mailbox in enumerate(self.mailboxes):
            self.mailboxes_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    mailbox.address,
                    ", ".join(mailbox.folders) or "All folders",
                    "Archive" if mailbox.archive_existing_messages else "Skip initially",
                    "Yes" if mailbox.enabled else "No",
                ),
            )
        if "authorization_frame" in self.__dict__:
            self._update_authorization()

    def _add_mailbox(self) -> None:
        dialog = MailboxDialog(
            self, address=self.variables["username"].get() if not self.mailboxes else ""
        )
        self.wait_window(dialog)
        self.grab_set()
        if dialog.result:
            self.mailboxes.append(dialog.result)
            self._refresh_mailboxes()

    def _edit_mailbox(self) -> None:
        selection = self.mailboxes_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        dialog = MailboxDialog(self, mailbox=self.mailboxes[index])
        self.wait_window(dialog)
        self.grab_set()
        if dialog.result:
            self.mailboxes[index] = dialog.result
            self._refresh_mailboxes()

    def _remove_mailbox(self) -> None:
        selection = self.mailboxes_tree.selection()
        if selection:
            del self.mailboxes[int(selection[0])]
            self._refresh_mailboxes()

    def _provider_changed(self) -> None:
        provider = PROVIDER_LABELS[self.variables["provider"].get()]
        self.variables["auth"].set(
            {
                MailProvider.GENERIC_IMAP: "Password",
                MailProvider.GMAIL_API: "Google OAuth - user sign-in",
                MailProvider.MICROSOFT_GRAPH: "Microsoft OAuth - delegated user access",
            }[provider]
        )
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
        self.mailboxes_frame.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.help_label.grid(
            row=row + 2,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 14),
        )
        self.authorization_frame.grid(
            row=row + 3, column=0, columnspan=2, sticky="ew", pady=(0, 14)
        )

    def _update_fields(self) -> None:
        provider = PROVIDER_LABELS[self.variables["provider"].get()]
        if provider == MailProvider.GENERIC_IMAP:
            allowed_auth = ["Password", "Microsoft OAuth (XOAUTH2)"]
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
        self._layout_fields(
            visible_fields,
            show_ssl=imap and auth == AuthMode.PASSWORD,
        )
        keep_suffix = " (leave blank to keep it)" if self.account else ""
        if imap and auth == AuthMode.PASSWORD:
            self.field_labels["secret"].configure(text="Password / app password" + keep_suffix)
            help_text = (
                "The password is stored in the operating system's credential store. "
                "Some IMAP providers require an app password. This login reads its own mailbox; "
                "add its address under Mailboxes."
            )
        elif imap:
            self.field_labels["tenant_id"].configure(
                text="Microsoft tenant / audience (blank uses common)"
            )
            help_text = (
                "Use Microsoft OAuth for Outlook.com or Microsoft 365 IMAP. MailArchive connects "
                "only to outlook.office365.com:993 with direct TLS so the bearer token cannot "
                "be sent to another server. Use the Authorization section to sign in through "
                "the system browser. Add your own or permitted shared mailbox "
                "addresses under Mailboxes."
            )
        elif google_application:
            help_text = (
                "For Google Workspace only. Select a service-account JSON key whose client ID "
                "has domain-wide delegation for gmail.readonly. The mailbox address is the "
                "Workspace user to impersonate. Add every target under Mailboxes; the same "
                "service-account key is used for all of them."
            )
        elif google:
            self.field_labels["client_id"].configure(text="Google OAuth desktop client ID")
            self.field_labels["secret"].configure(
                text="Google OAuth client secret (optional)" + keep_suffix
            )
            help_text = (
                "Enter the credentials of a Google OAuth client whose application type is "
                "Desktop app. Use the Authorization section to sign in through "
                "the system browser. The client secret and token data stay in the operating "
                "system's credential store. Add the signed-in address under Mailboxes."
            )
        elif auth == AuthMode.OAUTH_APPLICATION:
            self.field_labels["client_id"].configure(text="Microsoft application client ID")
            self.field_labels["tenant_id"].configure(text="Microsoft tenant ID")
            self.field_labels["secret"].configure(
                text="Microsoft OAuth client secret" + keep_suffix
            )
            help_text = (
                "Use a Microsoft Entra app registration with application Mail.Read permission "
                "and admin consent. Enter the tenant ID and client secret, then add each "
                "permitted address under Mailboxes."
            )
        else:
            self.field_labels["tenant_id"].configure(
                text="Microsoft tenant / audience (blank uses common)"
            )
            help_text = (
                "MailArchive uses its built-in Microsoft sign-in registration. The tenant can "
                "be a directory ID, organizations, consumers, or common. Use the Authorization "
                "section to sign in through the system browser. Add your own "
                "and permitted shared addresses under Mailboxes; authorize again after "
                "adding the first shared mailbox to grant shared read access."
            )
        self.help_label.configure(text=help_text)
        self._update_authorization()

    def _update_authorization(self) -> None:
        interactive = AUTH_LABELS.get(self.variables["auth"].get()) == AuthMode.OAUTH_USER
        self.retry_credentials_button.pack_forget()
        invalid_input = False
        try:
            submission = (
                self._submission()
                if interactive
                else AccountSubmission(
                    build_account_configuration(self._form_values(), existing=self.account),
                    {},
                    False,
                )
            )
            submission.account.id = self._account_id
            self._authorization_submission = submission
        except (KeyError, RuntimeError, ValueError):
            invalid_input = True
            submission = self._authorization_submission
        if self.editor and submission is not None:
            status, result = self.editor.authorization_snapshot(submission)
        else:
            status = account_status(
                self.account or Account("", auth_mode=AuthMode.OAUTH_USER),
                [],
                AuthorizationStatus(AuthorizationState.REQUIRED),
            )
            result = None
        if not interactive and (
            submission is None or status.authorization.state == AuthorizationState.NOT_REQUIRED
        ):
            self.authorization_frame.grid_remove()
            return
        self.authorization_frame.grid()
        self._layout_authorization_actions(interactive)
        self.authorization_label.configure(
            text=AUTHORIZATION_STATE_LABELS[status.authorization.state]
        )
        self.authorize_button.configure(
            text="Reauthorize"
            if status.authorization.state == AuthorizationState.AUTHORIZED
            else "Authorize",
            state="normal"
            if self.editor and status.allows(AccountAction.AUTHORIZE)
            else "disabled",
        )
        authorizing = status.allows(AccountAction.CANCEL_AUTHORIZATION)
        for key in ("provider", "auth"):
            self.widgets[key].configure(state="disabled" if authorizing else "readonly")
        self.cancel_authorization_button.configure(state="normal" if authorizing else "disabled")
        self.save_button.configure(state="disabled" if authorizing else "normal")
        if (
            self.account is not None
            and status.authorization.state == AuthorizationState.UNAVAILABLE
        ):
            self.retry_credentials_button.pack(side="left", padx=(6, 0))
        detail = (
            {
                AuthorizationOutcome.COMPLETED: "Authorization complete. Not saved yet — click Save to keep it.",
                AuthorizationOutcome.CANCELLED: "Authorization cancelled. You can try again.",
                AuthorizationOutcome.FAILED: "Authorization failed. You can try again.",
            }.get(result.outcome, "")
            if result
            else ""
        )
        if authorizing:
            detail = "Complete sign-in in your browser. Changes are not saved yet."
        elif invalid_input:
            detail = "Check the account inputs before authorizing or saving."
        self.authorization_detail.configure(
            text=self._authorization_error
            or (result.detail if result else "")
            or status.authorization.detail
            or detail
        )

    def _layout_authorization_actions(self, interactive: bool) -> None:
        self.authorization_frame.configure(
            text="Authorization" if interactive else "Saved credentials"
        )
        self.authorization_help.configure(
            text=(
                "Authorize opens your browser using these inputs and keeps this dialog open.\n"
                "Click Save to keep the account and authorization."
                if interactive
                else "Unlock the system credential store, then retry the credential check."
            )
        )
        if interactive:
            self.authorize_button.pack(side="left")
            self.cancel_authorization_button.pack(side="left", padx=(6, 0))
        else:
            self.authorize_button.pack_forget()
            self.cancel_authorization_button.pack_forget()

    def _form_values(self) -> AccountFormValues:
        return AccountFormValues(
            label=self.variables["label"].get(),
            provider=PROVIDER_LABELS[self.variables["provider"].get()],
            auth_mode=AUTH_LABELS[self.variables["auth"].get()],
            host=self.variables["host"].get(),
            port=self.variables["port"].get(),
            username=self.variables["username"].get(),
            secret=self.variables["secret"].get(),
            mailboxes=self.mailboxes,
            client_id=self.variables["client_id"].get(),
            tenant_id=self.variables["tenant_id"].get(),
            service_account_file=self.variables["service_account_file"].get(),
            poll_minutes=self.variables["poll"].get(),
            use_ssl=bool(self.variables["ssl"].get()),
            enabled=bool(self.variables["enabled"].get()),
        )

    def _submission(self) -> AccountSubmission:
        submission = build_account_submission(
            self._form_values(),
            existing=self.account,
            service_account_loader=self.read_service_account,
        )
        submission.account.id = self._account_id
        return submission

    def _authorize(self) -> None:
        try:
            submission = self._submission()
        except (KeyError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self._authorization_error = ""
        try:
            if self.editor is not None:
                self.editor.authorize(submission)
        except Exception as exc:
            self._authorization_error = f"Authorization failed: {exc}"
        self._update_authorization()

    def _cancel_authorization(self) -> None:
        if self.editor is not None:
            self.editor.cancel_authorization()

    def _retry_credentials(self) -> None:
        self._authorization_error = ""
        try:
            if self.editor is not None:
                self.editor.refresh_authorization()
        except Exception as exc:
            self._authorization_error = f"Credential check failed: {exc}"
        self._update_authorization()

    def _save(self) -> None:
        try:
            submission = self._submission()
        except (KeyError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        try:
            if self.editor is not None:
                self.editor.save(submission)
        except Exception as exc:
            messagebox.showerror("Email account not saved", str(exc), parent=self)
            return
        self.result = submission
        self.destroy()


class DestinationBlock(ttk.LabelFrame):
    """Edit one destination without changing the saved target."""

    def __init__(self, parent: DestinationsEditor, target: RuleTarget) -> None:
        super().__init__(parent.content, padding=10)
        self._variable_traces: tuple[tuple[tk.Variable, str], ...] = ()
        self.target_id = target.id
        self.path_var = tk.StringVar(master=self, value=target.path)
        self.preview_var = tk.StringVar(master=self)
        self.save_var = tk.StringVar(master=self, value=_label_for(SAVE_LABELS, target.save_mode))
        self.attachments_in_destination_var = tk.BooleanVar(
            master=self, value=target.attachments_in_destination
        )
        ttk.Label(self, text="Destination path").grid(row=0, column=0, sticky="w")
        self.path_entry = ttk.Entry(self, textvariable=self.path_var, width=32)
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Button(self, text="Choose folder", command=self._choose_folder).grid(
            row=0, column=2, padx=(6, 0)
        )
        ttk.Label(self, text="Preview").grid(row=1, column=0, sticky="nw", pady=3)
        self.preview_label = ttk.Label(
            self, textvariable=self.preview_var, foreground="#555555", wraplength=340
        )
        self.preview_label.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=3)
        _wrap_label_to_width(self.preview_label)
        ttk.Label(self, text="Save as").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(
            self, textvariable=self.save_var, values=list(SAVE_LABELS), state="readonly"
        ).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=3)
        self.attachments_in_destination_box = ttk.Checkbutton(
            self,
            text="Save attachments directly in destination folder",
            variable=self.attachments_in_destination_var,
        )
        self.attachments_in_destination_box.grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.remove_button = ttk.Button(self, text="Remove", command=lambda: parent.remove(self))
        self.remove_button.grid(row=3, column=2, sticky="e", pady=(6, 0))
        self.columnconfigure(1, weight=1)
        self._variable_traces = (
            (self.path_var, self.path_var.trace_add("write", self._update_preview)),
            (self.save_var, self.save_var.trace_add("write", self._update_attachment_option)),
        )
        self._update_preview()
        self._update_attachment_option()

    def destroy(self) -> None:
        for variable, handle in self._variable_traces:
            variable.trace_remove("write", handle)
        self._variable_traces = ()
        super().destroy()

    def target(self) -> RuleTarget:
        return RuleTarget(
            path=self.path_var.get(),
            save_mode=SAVE_LABELS[self.save_var.get()],
            attachments_in_destination=self.attachments_in_destination_var.get(),
            id=self.target_id,
        )

    def _choose_folder(self) -> None:
        selected = choose_destination_folder(self.winfo_toplevel(), self.path_var.get())
        if selected:
            self.path_var.set(selected)

    def _update_preview(self, *_args: str) -> None:
        path = self.path_var.get()
        try:
            self.preview_var.set(
                str(destination_path(path)) if path else "Enter a destination path."
            )
        except ValueError as exc:
            self.preview_var.set(f"Invalid destination: {exc}")

    def _update_attachment_option(self, *_args: str) -> None:
        self.attachments_in_destination_box.configure(
            state="disabled"
            if SAVE_LABELS[self.save_var.get()] == SaveMode.EMAIL_ONLY
            else "normal"
        )


class DestinationsEditor(ttk.LabelFrame):
    def __init__(self, parent: tk.Misc, targets: list[RuleTarget]) -> None:
        super().__init__(parent, text="Destinations", padding=10)
        self.blocks: list[DestinationBlock] = []
        self.path_hint = ttk.Label(
            self,
            text="Absolute paths; {year} and {month} use the reception date (preview: YYYY/MM).",
            foreground="#555555",
        )
        self.path_hint.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        _wrap_label_to_width(self.path_hint)
        self.scroll_area = ScrollableFrame(self, width=540)
        self.scroll_area.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.canvas = self.scroll_area.canvas
        self.content = self.scroll_area.content
        self.content.columnconfigure(0, weight=1)
        self.add_button = ttk.Button(self, text="Add destination", command=self.add)
        self.add_button.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        for target in targets or [RuleTarget("")]:
            self._append(target)
        self._update_blocks()
        self.scroll_area.bind_widgets()
        self.update_idletasks()
        self.canvas.configure(width=self.content.winfo_reqwidth())
        # Show about one and a half targets without growing as more are added.
        self.viewport_height = round((self.blocks[0].winfo_reqheight() + 6) * 1.5)

    def _append(self, target: RuleTarget) -> DestinationBlock:
        block = DestinationBlock(self, target)
        self.blocks.append(block)
        return block

    def add(self) -> None:
        block = self._append(RuleTarget(""))
        self._update_blocks()
        self.scroll_area.bind_widgets(block)
        self.focus_path(len(self.blocks) - 1)

    def remove(self, block: DestinationBlock) -> None:
        if len(self.blocks) == 1:
            return
        index = self.blocks.index(block)
        self.blocks.remove(block)
        block.destroy()
        self._update_blocks()
        self.focus_path(min(index, len(self.blocks) - 1))

    def _update_blocks(self) -> None:
        for index, block in enumerate(self.blocks):
            block.configure(text=f"Destination {index + 1}")
            block.grid(row=index, column=0, sticky="ew", pady=(0, 6))
            block.remove_button.configure(state="disabled" if len(self.blocks) == 1 else "normal")

    def focus_path(self, index: int) -> None:
        entry = self.blocks[index].path_entry
        self.see(entry)
        entry.focus_set()

    def see(self, widget: tk.Misc) -> None:
        self.scroll_area.see(widget)


class RuleDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        rule: Rule | None = None,
        *,
        accounts: list[Account] | None = None,
        save_rule: Callable[[Rule], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Edit rule" if rule else "Add rule")
        self.resizable(False, False)
        self.transient(parent)
        self.result: Rule | None = None
        self.rule = rule
        self.save_rule = save_rule
        self.condition_editor: ConditionsEditor | None = None
        condition = rule.conditions[0] if rule and rule.conditions else Condition()

        self.dialog_frame = ttk.Frame(self, padding=20)
        self.dialog_frame.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.dialog_frame.columnconfigure(0, weight=1)
        self.dialog_frame.rowconfigure(0, weight=1)
        self.form_scroll = ScrollableFrame(self.dialog_frame)
        self.form_scroll.grid(row=0, column=0, sticky="nsew")
        frame = self.form_scroll.content
        self.form_frame = frame
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)
        self.name_var = tk.StringVar(value=rule.name if rule else "")
        self.field_var = tk.StringVar(value=_label_for(FIELD_LABELS, condition.field))
        self.operator_var = tk.StringVar(value=_label_for(OPERATOR_LABELS, condition.operator))
        self.value_var = tk.StringVar(
            value=condition.value if condition.field != MailField.SENDER else ""
        )
        sender_values = [condition.value] if condition.field == MailField.SENDER else [""]
        if (
            rule
            and rule.match_mode == MatchMode.ANY
            and rule.conditions
            and all(
                item.field == MailField.SENDER and item.operator == condition.operator
                for item in rule.conditions
            )
        ):
            sender_values = [item.value for item in rule.conditions]
        self.sender_value_vars = [tk.StringVar(value=value) for value in sender_values]
        self.enabled_var = tk.BooleanVar(value=rule.enabled if rule else True)
        self.account_scope_var = tk.StringVar(
            value="selected" if rule and rule.account_ids is not None else "all"
        )
        self.account_options = rule_account_options(accounts or [], rule)

        ttk.Label(frame, text="Rule name").grid(row=0, column=0, sticky="w", pady=5)
        self.name_entry = ttk.Entry(frame, textvariable=self.name_var, width=42)
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        self._build_account_selection(frame)
        ttk.Separator(frame).grid(row=2, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="When").grid(row=3, column=0, sticky="w", pady=5)
        field_box = ttk.Combobox(
            frame,
            textvariable=self.field_var,
            values=list(FIELD_LABELS),
            state="readonly",
            width=22,
        )
        field_box.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)
        field_box.bind("<<ComboboxSelected>>", lambda event: self._update_fields())
        ttk.Label(frame, text="Comparison").grid(row=4, column=0, sticky="w", pady=5)
        self.operator_box = ttk.Combobox(
            frame,
            textvariable=self.operator_var,
            values=list(OPERATOR_LABELS),
            state="readonly",
            width=22,
        )
        self.operator_box.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        self.value_label = ttk.Label(frame, text="Values")
        self.value_label.grid(row=5, column=0, sticky="nw", pady=5)
        self.value_entry = ttk.Entry(frame, textvariable=self.value_var)
        self.value_entry.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)
        self.sender_fields_frame = ttk.Frame(frame)
        self.sender_fields_frame.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)
        self.sender_fields_frame.columnconfigure(0, weight=1)
        self.sender_scroll = ScrollableFrame(self.sender_fields_frame)
        self.sender_scroll.grid(row=0, column=0, sticky="nsew")
        self.sender_scroll.content.columnconfigure(0, weight=1)
        self.add_sender_button = ttk.Button(
            self.sender_fields_frame, text="Add another value", command=self._add_sender_field
        )
        self.add_sender_button.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._render_sender_fields()
        self.value_hint = ttk.Label(frame, text="", foreground="#555555")
        self.value_hint.grid(row=6, column=1, columnspan=2, sticky="ew")
        _wrap_label_to_width(self.value_hint)
        self._simple_matching_widgets = tuple(
            widget for widget in frame.grid_slaves() if 3 <= int(widget.grid_info()["row"]) <= 6
        )
        self.more_conditions_button = ttk.Button(
            frame, text="Add condition", command=self._show_conditions
        )
        self.more_conditions_button.grid(row=7, column=1, columnspan=2, sticky="w", pady=(6, 8))
        self.destinations = DestinationsEditor(frame, rule.targets if rule else [])
        self.destinations.grid(row=8, column=0, columnspan=3, sticky="nsew")
        ttk.Checkbutton(frame, text="Rule enabled", variable=self.enabled_var).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(8, 2)
        )
        rule_hint = ttk.Label(
            frame,
            text="The first matching rule for this email account is used.",
            foreground="#555555",
        )
        rule_hint.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(6, 8))
        _wrap_label_to_width(rule_hint)
        buttons = ttk.Frame(self.dialog_frame)
        buttons.grid(row=1, column=0, sticky="e", pady=(12, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="left", padx=5)
        self.save_button = ttk.Button(buttons, text="Save", command=self._save)
        self.save_button.pack(side="left")
        if rule is not None and not has_simple_matching(rule):
            self._show_conditions(deepcopy(rule.conditions), rule.match_mode)
        self.update_idletasks()
        self.form_scroll.bind_widgets()
        self.form_scroll.canvas.configure(
            width=min(self.form_frame.winfo_reqwidth(), self.winfo_screenwidth() - 106)
        )
        self.update_idletasks()
        self._fixed_width = min(self.winfo_reqwidth(), self.winfo_screenwidth() - 48)
        self._base_height = 0
        self._update_fields()
        self._initial_matching = self._current_matching()
        self._base_height = self.winfo_reqheight()
        self.bind("<Escape>", lambda event: self.destroy())
        _center_on_parent(
            self,
            parent,
            width=self._fixed_width,
            height=self._dialog_height,
            keep_visible=True,
        )
        self.deiconify()
        self.update_idletasks()
        self._fit_content_height()
        self.grab_set()
        self.name_entry.focus_set()

    def _build_account_selection(self, parent: tk.Misc) -> None:
        scope = ttk.LabelFrame(parent, text="Email accounts", padding=10)
        scope.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        scope.columnconfigure(0, weight=1)
        scope.columnconfigure(1, weight=1)
        for column, (label, value) in enumerate(
            (("All email accounts", "all"), ("Selected email accounts", "selected"))
        ):
            ttk.Radiobutton(
                scope,
                text=label,
                variable=self.account_scope_var,
                value=value,
                command=self._update_account_selection,
            ).grid(row=0, column=column, sticky="w")
        self.account_selection_frame = ttk.Frame(scope)
        self.account_selection_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.account_selection_frame.columnconfigure(0, weight=1)
        self.account_list = tk.Listbox(
            self.account_selection_frame,
            selectmode="multiple",
            exportselection=False,
            height=min(4, max(2, len(self.account_options))),
            width=42,
        )
        self.account_list.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(
            self.account_selection_frame, orient="vertical", command=self.account_list.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.account_list.configure(yscrollcommand=scrollbar.set)
        selected_ids = set(self.rule.account_ids or []) if self.rule else set()
        for index, (account_id, label) in enumerate(self.account_options):
            self.account_list.insert("end", label)
            if account_id in selected_ids:
                self.account_list.selection_set(index)
        account_hint = ttk.Label(
            self.account_selection_frame,
            text="Click to select one or more email accounts."
            if self.account_options
            else "Add an email account before choosing specific accounts.",
            foreground="#555555",
        )
        account_hint.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        _wrap_label_to_width(account_hint)
        self._update_account_selection()

    def _update_account_selection(self) -> None:
        if self.account_scope_var.get() == "all":
            self.account_selection_frame.grid_remove()
        else:
            self.account_selection_frame.grid()
        if "_fixed_width" in self.__dict__:
            self._fit_content_height()

    def _update_fields(self) -> None:
        if self.__dict__.get("condition_editor") is not None:
            self._fit_content_height()
            return
        field = FIELD_LABELS[self.field_var.get()]
        self.value_label.configure(text="Values" if field == MailField.SENDER else "Value")
        if field == MailField.SENDER:
            self.value_entry.grid_remove()
            self.sender_fields_frame.grid()
        else:
            self.sender_fields_frame.grid_remove()
            self.value_entry.grid()
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
                saved_attachment = (
                    self.rule
                    and self.rule.conditions
                    and (self.rule.conditions[0].field == MailField.HAS_ATTACHMENT)
                )
                if not self.value_var.get() and not saved_attachment:
                    self.value_var.set("Yes")
            elif field == MailField.SENDER:
                self.value_hint.configure(
                    text="Add one sender value per field. Any match is sufficient."
                )
            else:
                self.value_hint.configure(text="Matching is case-insensitive.")
        if "_fixed_width" in self.__dict__:
            self._fit_content_height()

    def _render_sender_fields(self) -> None:
        for child in self.sender_scroll.content.winfo_children():
            child.destroy()
        self.sender_entries = []
        for index, variable in enumerate(self.sender_value_vars):
            entry = ttk.Entry(self.sender_scroll.content, textvariable=variable, width=34)
            entry.grid(row=index, column=0, sticky="ew", pady=(0, 4))
            self.sender_entries.append(entry)
            ttk.Button(
                self.sender_scroll.content,
                text="Remove",
                command=lambda item=index: self._remove_sender_field(item),
                state="normal" if len(self.sender_value_vars) > 1 else "disabled",
            ).grid(row=index, column=1, padx=(6, 0), pady=(0, 4))
        self.sender_scroll.bind_widgets()
        self.update_idletasks()
        row_height = (
            max(child.winfo_reqheight() for child in self.sender_scroll.content.winfo_children())
            + 4
        )
        self.sender_scroll.canvas.configure(
            width=self.sender_scroll.content.winfo_reqwidth(),
            height=row_height * min(4, len(self.sender_value_vars)),
        )
        if "_fixed_width" in self.__dict__:
            self._fit_content_height()

    def _fit_content_height(self) -> None:
        if "_fixed_width" not in self.__dict__:
            return
        self.update_idletasks()
        max_height = self.winfo_screenheight() - 80
        canvas = self.destinations.canvas
        fixed_height = self.dialog_frame.winfo_reqheight() - self.form_scroll.winfo_reqheight()
        outside_height = fixed_height + self.form_frame.winfo_reqheight() - canvas.winfo_reqheight()
        viewport_height = min(
            self.destinations.viewport_height, max(80, max_height - outside_height)
        )
        canvas.configure(height=viewport_height)
        self.update_idletasks()
        self.form_scroll.canvas.configure(
            height=min(self.form_frame.winfo_reqheight(), max(1, max_height - fixed_height))
        )
        self.update_idletasks()
        self._dialog_height = min(max_height, max(self._base_height, self.winfo_reqheight()))
        self.geometry(f"{self._fixed_width}x{self._dialog_height}")
        if self.winfo_ismapped():
            top = self.winfo_vrooty()
            y = max(
                top + 24,
                min(self.winfo_y(), top + self.winfo_vrootheight() - self._dialog_height - 56),
            )
            if y != self.winfo_y():
                self.geometry(f"+{self.winfo_x()}+{y}")

    def _add_sender_field(self) -> None:
        self.sender_value_vars.append(tk.StringVar(master=self))
        self._render_sender_fields()
        self._focus_sender(len(self.sender_value_vars) - 1)

    def _remove_sender_field(self, index: int) -> None:
        if len(self.sender_value_vars) == 1:
            return
        self.sender_value_vars.pop(index)
        self._render_sender_fields()
        self._focus_sender(min(index, len(self.sender_value_vars) - 1))

    def _focus_sender(self, index: int) -> None:
        entry = self.sender_entries[index]
        self.sender_scroll.see(entry)
        entry.focus_set()

    def _current_matching(self) -> tuple[tuple[Condition, ...], MatchMode]:
        editor = self.__dict__.get("condition_editor")
        if editor is not None:
            return editor.matching()
        field = FIELD_LABELS[self.field_var.get()]
        operator = OPERATOR_LABELS[self.operator_var.get()]
        values = (
            [variable.get() for variable in self.sender_value_vars]
            if field == MailField.SENDER
            else [self.value_var.get()]
        )
        conditions = tuple(Condition(field, operator, value) for value in values)
        mode = (
            MatchMode.ANY
            if len(conditions) > 1
            else self.rule.match_mode
            if self.rule is not None
            else MatchMode.ALL
        )
        return conditions, mode

    def _show_conditions(
        self, conditions: list[Condition] | None = None, match_mode: MatchMode | None = None
    ) -> None:
        if self.condition_editor is not None:
            return
        if conditions is None:
            current, match_mode = self._current_matching()
            conditions = [*current, Condition(field=MailField.SUBJECT)]
        for widget in self._simple_matching_widgets:
            widget.grid_remove()
        self.more_conditions_button.grid_remove()
        self.condition_editor = ConditionsEditor(
            self.form_frame, conditions, match_mode or MatchMode.ALL, self._fit_content_height
        )
        self.condition_editor.grid(row=3, column=0, columnspan=3, rowspan=5, sticky="ew")
        self.form_scroll.bind_widgets(self.condition_editor)
        self._fit_content_height()

    def _matching_for_save(self) -> tuple[tuple[Condition, ...], MatchMode]:
        current = self._current_matching()
        if self.rule is not None and current == self.__dict__.get("_initial_matching"):
            return tuple(deepcopy(self.rule.conditions)), self.rule.match_mode
        return current

    def _save(self) -> None:
        try:
            conditions, match_mode = self._matching_for_save()
            rule = build_rule(
                RuleFormValues(
                    name=self.name_var.get(),
                    targets=tuple(block.target() for block in self.destinations.blocks),
                    field=FIELD_LABELS[self.field_var.get()],
                    operator=OPERATOR_LABELS[self.operator_var.get()],
                    value=self.value_var.get(),
                    sender_values=tuple(variable.get() for variable in self.sender_value_vars),
                    enabled=bool(self.enabled_var.get()),
                    all_accounts=self.account_scope_var.get() == "all",
                    selected_account_ids=tuple(
                        self.account_options[index][0] for index in self.account_list.curselection()
                    ),
                    conditions=conditions,
                    match_mode=match_mode,
                ),
                existing=self.rule,
            )
        except DestinationValidationError as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            self.destinations.focus_path(exc.index)
            return
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        try:
            if self.save_rule is not None:
                self.save_rule(rule)
        except Exception as exc:
            messagebox.showerror("Rules not saved", str(exc), parent=self)
            return
        self.result = rule
        self.destroy()
