from __future__ import annotations

import argparse
import os
from tkinter import messagebox

from mailarchive import __version__
from mailarchive.application.credential_port import CredentialStore
from mailarchive.bootstrap import create_application
from mailarchive.infrastructure.credentials import (
    KeyringCredentialStore,
    UnavailableCredentialStore,
    WindowsCredentialStore,
)
from mailarchive.infrastructure.linux_integration import AppImageIntegration
from mailarchive.infrastructure.platform_integration import SingleInstance
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.presentation.desktop import DesktopApp
from mailarchive.presentation.window import create_root


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
        # Exercise the bundled Tcl/Tk runtime and Pillow icon bridge. Imports
        # alone do not load Pillow's dynamically imported native Tk helpers.
        root = create_root()
        try:
            root.update()
            root.withdraw()
            root.update()
            root.deiconify()
            root.update()
        finally:
            root.destroy()
        return
    instance = SingleInstance()
    if instance.already_running:
        try:
            instance.activate()
        finally:
            instance.close()
        return
    try:
        root = create_root()
        config_store = ConfigStore()
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
        application = None
        try:
            application = create_application(config_store, credential_store)
            app = DesktopApp(root, application, AppImageIntegration.for_current_process())
            application.set_observers(app.on_service_event, app.on_run_progress)
            application.start()
        except Exception as exc:
            if application is not None:
                application.close()
            messagebox.showerror("MailArchive could not start", str(exc), parent=root)
            root.destroy()
            return
        if credential_warning:
            messagebox.showerror(
                "Credential storage unavailable",
                "Secure credential storage is unavailable: " + credential_warning,
                parent=root,
            )
        if arguments.minimized and app.tray.safe_to_hide:
            root.withdraw()
        if not arguments.minimized:
            app.offer_desktop_integration()
        instance.set_activation_handler(lambda: app.post_ui(app.show))
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
