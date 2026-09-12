from __future__ import annotations

import tkinter as tk
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
    MailField,
    MailProvider,
    MatchMode,
    Rule,
    SaveMode,
)
from mailarchive.oauth import parse_google_service_account_file
from mailarchive.storage import destination_path
from mailarchive.ui_text import (
    AUTH_LABELS,
    FIELD_LABELS,
    OPERATOR_LABELS,
    PROVIDER_LABELS,
    SAVE_LABELS,
    _auth_label_for,
    _label_for,
)


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
        self.result: AccountSubmission | None = None
        self.account = account
        self.default_poll_minutes = default_poll_minutes
        self.transient(parent)
        self.grab_set()

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
            "archive_existing": tk.BooleanVar(
                value=account.archive_existing_messages if account else False
            ),
        }
        self.widgets: dict[str, ttk.Widget] = {}
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
        self.archive_existing_check = ttk.Checkbutton(
            frame,
            text="Archive messages that already exist in this mailbox",
            variable=self.variables["archive_existing"],
        )
        self.archive_existing_check.grid(
            row=row + 2,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(6, 0),
        )
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
        self.archive_existing_check.grid(
            row=row + 1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(6, 0),
        )
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
                "Some IMAP providers require an app password."
            )
        elif imap:
            self.field_labels["tenant_id"].configure(
                text="Microsoft tenant / audience (blank uses common)"
            )
            help_text = (
                "Use Microsoft OAuth for Outlook.com or Microsoft 365 IMAP. MailArchive connects "
                "only to outlook.office365.com:993 with direct TLS so the bearer token cannot "
                "be sent to another server. Save the account, select it, and choose Authorize "
                "to sign in through the system browser. No password or token is entered here."
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
            self.field_labels["client_id"].configure(text="Microsoft application client ID")
            self.field_labels["tenant_id"].configure(text="Microsoft tenant ID")
            self.field_labels["secret"].configure(
                text="Microsoft OAuth client secret" + keep_suffix
            )
            help_text = (
                "Use a Microsoft Entra app registration with application Mail.Read permission "
                "and admin consent. Enter the tenant ID and client secret."
            )
        else:
            self.field_labels["tenant_id"].configure(
                text="Microsoft tenant / audience (blank uses common)"
            )
            help_text = (
                "MailArchive uses its built-in Microsoft sign-in registration. The tenant can "
                "be a directory ID, organizations, consumers, or common. Save the account, "
                "then choose Authorize to sign in through the system browser."
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
                    folder=self.variables["folder"].get(),
                    client_id=self.variables["client_id"].get(),
                    tenant_id=self.variables["tenant_id"].get(),
                    service_account_file=self.variables["service_account_file"].get(),
                    poll_minutes=self.variables["poll"].get(),
                    use_ssl=bool(self.variables["ssl"].get()),
                    enabled=bool(self.variables["enabled"].get()),
                    archive_existing_messages=bool(self.variables["archive_existing"].get()),
                ),
                existing=self.account,
                service_account_loader=parse_google_service_account_file,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
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
        self.destination_var = tk.StringVar(value=rule.destination if rule else "")
        self.save_var = tk.StringVar(
            value=_label_for(
                SAVE_LABELS, rule.save_mode if rule else SaveMode.EMAIL_AND_ATTACHMENTS
            )
        )
        self.enabled_var = tk.BooleanVar(value=rule.enabled if rule else True)
        self.archive_root = archive_root

        ttk.Label(frame, text="Rule name").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.name_var, width=42).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=5
        )
        ttk.Separator(frame).grid(row=1, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="When").grid(row=2, column=0, sticky="w", pady=5)
        field_box = ttk.Combobox(
            frame,
            textvariable=self.field_var,
            values=list(FIELD_LABELS),
            state="readonly",
            width=22,
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
        self.value_label = ttk.Label(frame, text="Value")
        self.value_label.grid(row=4, column=0, sticky="nw", pady=5)
        self.value_entry = ttk.Entry(frame, textvariable=self.value_var)
        self.value_entry.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        self.sender_fields_frame = ttk.Frame(frame)
        self.sender_fields_frame.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        self.sender_fields_frame.columnconfigure(0, weight=1)
        self._render_sender_fields()
        self.value_hint = ttk.Label(frame, text="", foreground="#555555")
        self.value_hint.grid(row=5, column=1, columnspan=2, sticky="w")
        ttk.Separator(frame).grid(row=6, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="Save to").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.destination_var).grid(
            row=7, column=1, sticky="ew", pady=5
        )
        ttk.Button(frame, text="Folder...", command=self._choose_folder).grid(
            row=7, column=2, padx=(6, 0)
        )
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
        self.value_label.configure(text="Email addresses" if field == MailField.SENDER else "Value")
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
                self.value_hint.configure(text="Add one sender email address per field.")
            else:
                self.value_hint.configure(text="Matching is case-insensitive.")

    def _render_sender_fields(self) -> None:
        for child in self.sender_fields_frame.winfo_children():
            child.destroy()
        for index, variable in enumerate(self.sender_value_vars):
            ttk.Entry(self.sender_fields_frame, textvariable=variable, width=34).grid(
                row=index, column=0, sticky="ew", pady=(0, 4)
            )
            if len(self.sender_value_vars) > 1:
                ttk.Button(
                    self.sender_fields_frame,
                    text="Remove",
                    command=lambda item=index: self._remove_sender_field(item),
                ).grid(row=index, column=1, padx=(6, 0), pady=(0, 4))
        ttk.Button(
            self.sender_fields_frame,
            text="Add another email",
            command=self._add_sender_field,
        ).grid(row=len(self.sender_value_vars), column=0, columnspan=2, sticky="w")

    def _add_sender_field(self) -> None:
        self.sender_value_vars.append(tk.StringVar(master=self))
        self._render_sender_fields()

    def _remove_sender_field(self, index: int) -> None:
        if len(self.sender_value_vars) == 1:
            return
        self.sender_value_vars.pop(index)
        self._render_sender_fields()

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
            if field not in {MailField.ALL, MailField.HAS_ATTACHMENT, MailField.SENDER} and not value:
                raise ValueError("Enter a comparison value.")
            if field == MailField.HAS_ATTACHMENT and value.casefold() not in {
                "yes",
                "no",
                "true",
                "false",
                "1",
                "0",
            }:
                raise ValueError('For "Has attachments", enter Yes or No.')
            operator = OPERATOR_LABELS[self.operator_var.get()]
            if field == MailField.SENDER:
                sender_values = [variable.get().strip() for variable in self.sender_value_vars]
                if any(not sender_value for sender_value in sender_values):
                    raise ValueError("Enter an email address in each sender field or remove it.")
                conditions = [
                    Condition(field=field, operator=operator, value=sender_value)
                    for sender_value in sender_values
                ]
            else:
                conditions = [Condition(field=field, operator=operator, value=value)]
            self.result = Rule(
                id=self.rule.id if self.rule else Rule("x", "x").id,
                name=name,
                destination=destination,
                conditions=conditions,
                save_mode=SAVE_LABELS[self.save_var.get()],
                match_mode=MatchMode.ANY if len(conditions) > 1 else MatchMode.ALL,
                enabled=bool(self.enabled_var.get()),
            )
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Check your input", str(exc), parent=self)
            return
        self.destroy()
