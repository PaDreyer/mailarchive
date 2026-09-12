from __future__ import annotations

import argparse
import os
import sqlite3
import tkinter as tk
from tkinter import messagebox

from mailarchive import __version__
from mailarchive.account_form import visible_account_fields
from mailarchive.config import ConfigStore
from mailarchive.credentials import (
    CredentialStore,
    KeyringCredentialStore,
    UnavailableCredentialStore,
    WindowsCredentialStore,
)
from mailarchive.desktop import DesktopApp
from mailarchive.dialogs import AccountDialog, RuleDialog
from mailarchive.migrations import DatabaseMigrationError
from mailarchive.platform_integration import SingleInstance, activate_existing_window
from mailarchive.service import EventLevel, ServiceEvent
from mailarchive.tray import TrayController
from mailarchive.ui_text import (
    AUTH_LABELS,
    FIELD_LABELS,
    OPERATOR_LABELS,
    PROVIDER_LABELS,
    SAVE_LABELS,
    _auth_label_for,
    _condition_summary,
    _label_for,
)

__all__ = [
    "AUTH_LABELS",
    "FIELD_LABELS",
    "OPERATOR_LABELS",
    "PROVIDER_LABELS",
    "SAVE_LABELS",
    "AccountDialog",
    "DesktopApp",
    "RuleDialog",
    "TrayController",
    "_auth_label_for",
    "_condition_summary",
    "_label_for",
    "main",
    "visible_account_fields",
]


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MailArchive")
    parser.add_argument("--version", action="version", version=f"MailArchive {__version__}")
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start in the notification area",
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    if arguments.smoke_test:
        return
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
            root.destroy()
            return
        credential_store: CredentialStore
        credential_warning: str | None = None
        if os.name == "nt":
            credential_store = WindowsCredentialStore()
        else:
            try:
                credential_store = KeyringCredentialStore()
            except Exception as exc:
                credential_warning = str(exc)
                credential_store = UnavailableCredentialStore(credential_warning)
        try:
            app = DesktopApp(root, config_store, settings, credential_store)
        except (DatabaseMigrationError, sqlite3.Error, OSError) as exc:
            messagebox.showerror("MailArchive could not start", str(exc), parent=root)
            root.destroy()
            return
        if credential_warning:
            app.on_service_event(
                ServiceEvent(
                    EventLevel.ERROR,
                    "Secure credential storage is unavailable: " + credential_warning,
                )
            )
        if arguments.minimized and app.tray.safe_to_hide:
            root.withdraw()
        if not arguments.minimized:
            app.offer_desktop_integration()
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
