from __future__ import annotations

import tkinter as tk
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mailarchive.account_form import (
    AccountFormValues,
    AccountSubmission,
    build_account_submission,
    visible_account_fields,
)
from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    DateFolderPosition,
    Mailbox,
    MailField,
    MailProvider,
    MatchMode,
    Rule,
    RuleTarget,
    SaveMode,
)
from mailarchive.oauth import parse_google_service_account_file
from mailarchive.rule_form import RuleFormValues, build_rule, rule_account_options
from mailarchive.storage import destination_path
from mailarchive.time_ranges import local_days_to_utc
from mailarchive.ui_text import (
    AUTH_LABELS,
    DATE_FOLDER_LABELS,
    FIELD_LABELS,
    OPERATOR_LABELS,
    PROVIDER_LABELS,
    SAVE_LABELS,
    _auth_label_for,
    _label_for,
)


@dataclass(frozen=True, slots=True)
class RangeSelection:
    source_id: str
    folders: set[str]
    start: datetime | None
    end: datetime | None
    timezone_name: str


class RangeDialog(tk.Toplevel):
    def __init__(
        self, parent: tk.Misc, accounts: list[Account], rules: list[Rule], timezone_name: str
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Archive existing messages")
        self.transient(parent)
        self.resizable(True, True)
        self.result: RangeSelection | None = None
        self.sources = [
            (account, mailbox)
            for account in accounts
            if account.enabled
            for mailbox in account.mailboxes
            if mailbox.enabled
        ]
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Mailbox source").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        self.source_labels = []
        for account, mailbox in self.sources:
            provider = _label_for(PROVIDER_LABELS, account.provider)
            if account.provider == MailProvider.GENERIC_IMAP:
                provider += f", {account.host}:{account.port}"
            self.source_labels.append(f"{account.label}: {mailbox.address} ({provider})")
        self.source_box = ttk.Combobox(
            frame, textvariable=self.source_var, values=self.source_labels, state="readonly"
        )
        self.source_box.grid(row=1, column=0, sticky="ew", pady=5)
        if self.source_labels:
            self.source_box.current(0)
        self.source_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_folders())
        ttk.Label(frame, text="Folders / labels (select one or more)").grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        folder_frame = ttk.Frame(frame)
        folder_frame.grid(row=3, column=0, sticky="nsew", pady=5)
        self.folder_list = tk.Listbox(
            folder_frame, selectmode="multiple", exportselection=False, height=6, width=60
        )
        scrollbar = ttk.Scrollbar(folder_frame, command=self.folder_list.yview)
        self.folder_list.configure(yscrollcommand=scrollbar.set)
        self.folder_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._refresh_folders()
        ttk.Label(frame, text="Received from (YYYY-MM-DD; blank = earliest)").grid(
            row=4, column=0, sticky="w", pady=(10, 0)
        )
        self.start_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.start_var).grid(row=5, column=0, sticky="ew")
        ttk.Label(frame, text="Through (YYYY-MM-DD, inclusive; blank = latest)").grid(
            row=6, column=0, sticky="w", pady=(10, 0)
        )
        self.end_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.end_var).grid(row=7, column=0, sticky="ew")
        ttk.Label(frame, text="Timezone for those days").grid(
            row=8, column=0, sticky="w", pady=(10, 0)
        )
        self.zone_var = tk.StringVar(value=timezone_name)
        ttk.Entry(frame, textvariable=self.zone_var).grid(row=9, column=0, sticky="ew")
        paths = [target.path for rule in rules for target in rule.targets]
        sample = "; ".join(paths[:3]) + (" ..." if len(paths) > 3 else "")
        ttk.Label(
            frame,
            text=f"Current rule order: {len(rules)} rules. Example destinations: {sample or 'none'}",
            wraplength=570,
        ).grid(row=10, column=0, sticky="w", pady=(12, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Start", command=self._save).pack(side="left")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        self.bind("<Escape>", lambda _event: self.destroy())
        _center_on_parent(self, parent)
        self.deiconify()
        self.grab_set()

    def _refresh_folders(self) -> None:
        self.folder_list.delete(0, "end")
        if not self.sources:
            return
        index = self.source_index()
        for folder in self.sources[index][1].folders:
            self.folder_list.insert("end", folder)
        self.folder_list.selection_set(0, "end")

    def source_index(self) -> int:
        index = self.source_box.current()
        if not 0 <= index < len(self.sources):
            raise ValueError("Select a mailbox source.")
        return index

    def _save(self) -> None:
        if not self.sources:
            messagebox.showerror("No source", "Add and enable a mailbox first.", parent=self)
            return
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
            source_index = self.source_index()
            _account, mailbox = self.sources[source_index]
            selected = {mailbox.folders[index] for index in self.folder_list.curselection()}
            if mailbox.folders and not selected:
                raise ValueError("Select at least one folder or label.")
            summary = (
                f"Source: {self.source_labels[source_index]}\n"
                f"Folders: {', '.join(selected) or 'all'}\n"
                f"Timezone: {self.zone_var.get().strip()}\n"
                f"UTC range: {start or 'earliest'} through {end or 'latest'} (exclusive)\n"
                "Current rules will be applied; open plans keep their existing rules."
            )
            if not messagebox.askyesno("Start range run?", summary, parent=self):
                return
            self.result = RangeSelection(
                mailbox.id, selected, start, end, self.zone_var.get().strip()
            )
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


def _center_on_parent(
    dialog: tk.Toplevel,
    parent: tk.Misc,
    *,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Position a hidden dialog over its parent using desktop coordinates."""
    parent.update_idletasks()
    dialog.update_idletasks()
    width = dialog.winfo_reqwidth() if width is None else width
    height = dialog.winfo_reqheight() if height is None else height
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
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
        self.enabled = tk.BooleanVar(value=mailbox.enabled if mailbox else True)
        ttk.Label(
            frame,
            text="Existing mail is skipped by automatic monitoring. Use a range run to archive it.",
        ).pack(anchor="w")
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
                archive_existing_messages=False,
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
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Edit email account" if account else "Add email account")
        self.resizable(False, False)
        self.result: AccountSubmission | None = None
        self.account = account
        self.mailboxes = deepcopy(account.mailboxes) if account else []
        self.default_poll_minutes = default_poll_minutes
        self.transient(parent)

        frame = ttk.Frame(self, padding=20)
        frame.grid(sticky="nsew")
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
        self.mailboxes_frame = ttk.LabelFrame(frame, text="Mailboxes", padding=8)
        self.mailboxes_frame.grid(row=row + 2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        mailbox_list = ttk.Frame(self.mailboxes_frame)
        mailbox_list.pack(fill="x")
        self.mailboxes_tree = ttk.Treeview(
            mailbox_list,
            columns=("address", "folders", "enabled"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        for key, title, width in (
            ("address", "Address", 190),
            ("folders", "Folders / labels", 180),
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
            ("Add...", self._add_mailbox),
            ("Edit...", self._edit_mailbox),
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
        self.buttons = ttk.Frame(frame)
        self.buttons.grid(row=row + 4, column=0, columnspan=2, sticky="e")
        ttk.Button(self.buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(self.buttons, text="Save", command=self._save).pack(side="left")
        self.bind("<Return>", lambda event: self._save())
        self.bind("<Escape>", lambda event: self.destroy())
        width, height = self._fix_size_for_layouts()
        _center_on_parent(self, parent, width=width, height=height)
        self.deiconify()
        self.update_idletasks()
        self.grab_set()
        self.widgets["label"].focus_set()

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
            width = max(width, self.winfo_reqwidth())
            height = max(height, self.winfo_reqheight())
        self.variables["provider"].set(original_provider)
        self.variables["auth"].set(original_auth)
        self._update_fields()
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
                    "Yes" if mailbox.enabled else "No",
                ),
            )

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
        self.buttons.grid(row=row + 3, column=0, columnspan=2, sticky="e")

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
                "be sent to another server. Save the account, select it, and choose Authorize "
                "to sign in through the system browser. Add your own or permitted shared mailbox "
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
                "Desktop app. Save the account, then choose Authorize to sign in through "
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
                "be a directory ID, organizations, consumers, or common. Save the account, "
                "then choose Authorize to sign in through the system browser. Add your own "
                "and permitted shared addresses under Mailboxes; authorize again after "
                "adding the first shared mailbox to grant shared read access."
            )
        self.help_label.configure(text=help_text)

    def _save(self) -> None:
        try:
            self.result = build_account_submission(
                AccountFormValues(
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
                ),
                existing=self.account,
                service_account_loader=parse_google_service_account_file,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.destroy()


class RuleTargetDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, target: RuleTarget | None = None) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Edit destination" if target else "Add destination")
        self.transient(parent)
        self.resizable(True, False)
        self.result: RuleTarget | None = None
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        self.path_var = tk.StringVar(value=target.path if target else "")
        self.mode_var = tk.StringVar(
            value=_label_for(SAVE_LABELS, target.save_mode) if target else "Email and attachments"
        )
        self.direct_var = tk.BooleanVar(
            value=target.attachments_in_destination if target else False
        )
        ttk.Label(frame, text="Full destination path; {year} and {month} are available").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Entry(frame, textvariable=self.path_var, width=60).grid(
            row=1, column=0, sticky="ew", pady=6
        )
        ttk.Button(frame, text="Folder...", command=self._choose).grid(row=1, column=1, padx=5)
        ttk.Label(frame, text="Save as").grid(row=2, column=0, sticky="w", pady=(12, 3))
        ttk.Combobox(
            frame, textvariable=self.mode_var, values=list(SAVE_LABELS), state="readonly"
        ).grid(row=3, column=0, sticky="ew")
        ttk.Checkbutton(
            frame, text="Save attachments directly in this destination", variable=self.direct_var
        ).grid(row=4, column=0, sticky="w", pady=12)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Save", command=self._save).pack(side="left")
        frame.columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _event: self.destroy())
        _center_on_parent(self, parent)
        self.deiconify()
        self.grab_set()

    def _choose(self) -> None:
        selected = filedialog.askdirectory(parent=self)
        if selected:
            self.path_var.set(selected)

    def _save(self) -> None:
        try:
            destination_path(Path(), self.path_var.get())
            self.result = RuleTarget(
                self.path_var.get(), SAVE_LABELS[self.mode_var.get()], self.direct_var.get()
            )
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.destroy()


class AdditionalTargetsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, targets: list[RuleTarget]) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Additional destinations")
        self.transient(parent)
        self.resizable(True, True)
        self.targets = deepcopy(targets)
        self.result: list[RuleTarget] | None = None
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(frame, width=75, height=8, exportselection=False)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="w", pady=8)
        ttk.Button(buttons, text="Add", command=self._add).pack(side="left")
        ttk.Button(buttons, text="Edit", command=self._edit).pack(side="left", padx=5)
        ttk.Button(buttons, text="Remove", command=self._remove).pack(side="left")
        ttk.Button(frame, text="Done", command=self._done).grid(row=2, column=0, sticky="e")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self._refresh()
        _center_on_parent(self, parent)
        self.deiconify()
        self.grab_set()

    def _refresh(self) -> None:
        self.listbox.delete(0, "end")
        for target in self.targets:
            self.listbox.insert(
                "end", f"{target.path} — {_label_for(SAVE_LABELS, target.save_mode)}"
            )

    def _selected(self) -> int | None:
        selection = self.listbox.curselection()
        return selection[0] if selection else None

    def _add(self) -> None:
        dialog = RuleTargetDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.targets.append(dialog.result)
            self._refresh()

    def _edit(self) -> None:
        index = self._selected()
        if index is None:
            return
        dialog = RuleTargetDialog(self, self.targets[index])
        self.wait_window(dialog)
        if dialog.result:
            dialog.result.id = self.targets[index].id
            self.targets[index] = dialog.result
            self._refresh()

    def _remove(self) -> None:
        index = self._selected()
        if index is not None:
            del self.targets[index]
            self._refresh()

    def _done(self) -> None:
        self.result = self.targets
        self.destroy()


class RuleDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        archive_root: str,
        rule: Rule | None = None,
        *,
        accounts: list[Account] | None = None,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Edit rule" if rule else "Add rule")
        self.resizable(False, False)
        self.transient(parent)
        self.result: Rule | None = None
        self.rule = rule
        self.additional_targets = deepcopy(rule.targets[1:]) if rule else []
        first_target = rule.targets[0] if rule and rule.targets else None
        condition = rule.conditions[0] if rule and rule.conditions else Condition()

        frame = ttk.Frame(self, padding=20)
        frame.grid(sticky="nsew")
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
        self.destination_var = tk.StringVar(value=first_target.path if first_target else "")
        self.date_folder_var = tk.StringVar(
            value=_label_for(
                DATE_FOLDER_LABELS,
                rule.date_folder_position if rule else DateFolderPosition.NONE,
            )
        )
        self.destination_preview_var = tk.StringVar()
        self.save_var = tk.StringVar(
            value=_label_for(
                SAVE_LABELS,
                first_target.save_mode if first_target else SaveMode.EMAIL_AND_ATTACHMENTS,
            )
        )
        self.attachments_in_destination_var = tk.BooleanVar(
            value=first_target.attachments_in_destination if first_target else False
        )
        self.enabled_var = tk.BooleanVar(value=rule.enabled if rule else True)
        self.archive_root = archive_root
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
        self._render_sender_fields()
        self.value_hint = ttk.Label(frame, text="", foreground="#555555")
        self.value_hint.grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Separator(frame).grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="First full destination").grid(row=8, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.destination_var).grid(
            row=8, column=1, sticky="ew", pady=5
        )
        ttk.Button(frame, text="Folder...", command=self._choose_folder).grid(
            row=8, column=2, padx=(6, 0)
        )
        ttk.Label(
            frame,
            text="Use an absolute path. {year} and {month} use the provider reception date.",
            foreground="#555555",
            wraplength=340,
        ).grid(row=9, column=1, columnspan=2, sticky="w", pady=(0, 5))
        ttk.Label(frame, text="Destination preview").grid(row=11, column=0, sticky="nw", pady=5)
        ttk.Label(
            frame,
            textvariable=self.destination_preview_var,
            foreground="#555555",
            wraplength=340,
        ).grid(row=11, column=1, columnspan=2, sticky="w", pady=5)
        ttk.Label(
            frame,
            text="Preview shows placeholders until a message's reception date is known.",
            foreground="#555555",
            wraplength=340,
        ).grid(row=12, column=1, columnspan=2, sticky="w", pady=(0, 5))
        self.destination_var.trace_add("write", self._update_destination_preview)
        self.date_folder_var.trace_add("write", self._update_destination_preview)
        self._update_destination_preview()
        ttk.Label(frame, text="Save as").grid(row=13, column=0, sticky="w", pady=5)
        ttk.Combobox(
            frame, textvariable=self.save_var, values=list(SAVE_LABELS), state="readonly"
        ).grid(row=13, column=1, columnspan=2, sticky="ew", pady=5)
        self.attachments_in_destination_box = ttk.Checkbutton(
            frame,
            text="Save attachments directly in destination folder",
            variable=self.attachments_in_destination_var,
        )
        self.attachments_in_destination_box.grid(
            row=14, column=1, columnspan=2, sticky="w", pady=(8, 2)
        )
        self.additional_summary = tk.StringVar()
        ttk.Button(
            frame, text="Additional destinations...", command=self._edit_additional_targets
        ).grid(row=15, column=1, sticky="w", pady=5)
        ttk.Label(frame, textvariable=self.additional_summary).grid(row=15, column=2, sticky="w")
        self._update_additional_summary()
        self.save_var.trace_add("write", self._update_attachment_option)
        self._update_attachment_option()
        ttk.Checkbutton(frame, text="Rule enabled", variable=self.enabled_var).grid(
            row=16, column=1, columnspan=2, sticky="w", pady=(8, 2)
        )
        ttk.Label(
            frame,
            text="The first matching rule for this email account is used.",
            foreground="#555555",
        ).grid(row=17, column=0, columnspan=3, sticky="w", pady=(8, 14))
        buttons = ttk.Frame(frame)
        buttons.grid(row=18, column=0, columnspan=3, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(buttons, text="Save", command=self._save).pack(side="left")
        self.update_idletasks()
        self._fixed_width = self.winfo_reqwidth()
        self._base_height = self.winfo_reqheight()
        self._update_fields()
        self.bind("<Escape>", lambda event: self.destroy())
        _center_on_parent(
            self,
            parent,
            width=self._fixed_width,
            height=max(self._base_height, self.winfo_reqheight()),
        )
        self.deiconify()
        self.update_idletasks()
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
        ttk.Label(
            self.account_selection_frame,
            text="Click to select one or more email accounts."
            if self.account_options
            else "Add an email account before choosing specific accounts.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._update_account_selection()

    def _update_account_selection(self) -> None:
        if self.account_scope_var.get() == "all":
            self.account_selection_frame.grid_remove()
        else:
            self.account_selection_frame.grid()
        if "_fixed_width" in self.__dict__:
            self._fit_content_height()

    def _update_fields(self) -> None:
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
                if not self.value_var.get():
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
        for child in self.sender_fields_frame.winfo_children():
            child.destroy()
        for index, variable in enumerate(self.sender_value_vars):
            ttk.Entry(self.sender_fields_frame, textvariable=variable, width=34).grid(
                row=index, column=0, sticky="ew", pady=(0, 4)
            )
            ttk.Button(
                self.sender_fields_frame,
                text="Remove",
                command=lambda item=index: self._remove_sender_field(item),
                state="normal" if len(self.sender_value_vars) > 1 else "disabled",
            ).grid(row=index, column=1, padx=(6, 0), pady=(0, 4))
        ttk.Button(
            self.sender_fields_frame,
            text="Add another value",
            command=self._add_sender_field,
        ).grid(row=len(self.sender_value_vars), column=0, columnspan=2, sticky="w")
        if "_fixed_width" in self.__dict__:
            self._fit_content_height()

    def _fit_content_height(self) -> None:
        self.update_idletasks()
        height = max(self._base_height, self.winfo_reqheight())
        self.geometry(f"{self._fixed_width}x{height}")

    def _add_sender_field(self) -> None:
        self.sender_value_vars.append(tk.StringVar(master=self))
        self._render_sender_fields()

    def _remove_sender_field(self, index: int) -> None:
        if len(self.sender_value_vars) == 1:
            return
        self.sender_value_vars.pop(index)
        self._render_sender_fields()

    def _update_attachment_option(self, *_args: str) -> None:
        self.attachments_in_destination_box.configure(
            state="disabled"
            if SAVE_LABELS[self.save_var.get()] == SaveMode.EMAIL_ONLY
            else "normal"
        )

    def _update_destination_preview(self, *_args: str) -> None:
        try:
            path = destination_path(
                Path(self.archive_root),
                self.destination_var.get(),
                DATE_FOLDER_LABELS[self.date_folder_var.get()],
            )
            self.destination_preview_var.set(str(path))
        except (ValueError, KeyError) as exc:
            self.destination_preview_var.set(f"Invalid destination: {exc}")
        if "_fixed_width" in self.__dict__:
            self._fit_content_height()

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self)
        if not selected:
            return
        self.destination_var.set(selected)

    def _update_additional_summary(self) -> None:
        self.additional_summary.set(f"{len(self.additional_targets)} additional")

    def _edit_additional_targets(self) -> None:
        dialog = AdditionalTargetsDialog(self, self.additional_targets)
        self.wait_window(dialog)
        if dialog.result is not None:
            self.additional_targets = dialog.result
            self._update_additional_summary()

    def _save(self) -> None:
        try:
            self.result = build_rule(
                RuleFormValues(
                    name=self.name_var.get(),
                    destination=self.destination_var.get(),
                    date_folder_position=DATE_FOLDER_LABELS[self.date_folder_var.get()],
                    field=FIELD_LABELS[self.field_var.get()],
                    operator=OPERATOR_LABELS[self.operator_var.get()],
                    value=self.value_var.get(),
                    sender_values=tuple(variable.get() for variable in self.sender_value_vars),
                    save_mode=SAVE_LABELS[self.save_var.get()],
                    attachments_in_destination=bool(self.attachments_in_destination_var.get()),
                    enabled=bool(self.enabled_var.get()),
                    all_accounts=self.account_scope_var.get() == "all",
                    selected_account_ids=tuple(
                        self.account_options[index][0] for index in self.account_list.curselection()
                    ),
                ),
                archive_root=Path(self.archive_root),
                existing=self.rule,
            )
            self.result.targets.extend(self.additional_targets)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.destroy()
