"""A credential-store recovery stays local and separate from saving the account."""

import json
import tkinter as tk
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_status import AuthorizationState, AuthorizationStatus
from mailarchive.domain.configuration import Account, AuthMode, Mailbox, MailProvider
from mailarchive.presentation.dialogs import AccountDialog
from tests import test_credential_admission_regressions as credential_fixture
from tests.tk_test_case import TkTestCase


class CredentialReinspectionDialogTests(TkTestCase):
    def setUp(self):
        fixture = credential_fixture.CredentialAdmissionRegressionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.credentials = credential_fixture.NativeSecretStore(fixture)
        self.app = fixture.application(self.credentials)
        self.account = fixture.save_password(self.app)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_password_reinspection_has_no_browser_actions_and_preserves_unsaved_changes(self):
        before = self.app.settings.to_dict()
        self.app.account_statuses.credential_record_failed(
            self.account.id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, "Store locked")
        )
        dialog = AccountDialog(
            self.root, 5, self.account, editor=self.app.account_editor(self.account.id)
        )
        self.addCleanup(dialog.destroy)
        dialog.variables["label"].set("Unsaved label")
        self.root.update()
        self.assertEqual(dialog.authorization_frame.cget("text"), "Saved credentials")
        self.assertFalse(dialog.authorize_button.winfo_manager())
        self.assertFalse(dialog.cancel_authorization_button.winfo_manager())
        self.assertTrue(dialog.retry_credentials_button.winfo_manager())
        self.assertNotIn("browser", dialog.authorization_help.cget("text").lower())
        retained = self.credentials.values.pop(self.account.id)
        with patch.object(self.app, "_authorize") as authorize:
            dialog.retry_credentials_button.invoke()
            self.wait_for_ui(
                lambda: "missing" in dialog.authorization_detail.cget("text").lower(),
                "The missing password remains visible after reinspection",
            )
            self.assertTrue(dialog.retry_credentials_button.winfo_manager())
            self.assertEqual(self.app.settings.to_dict(), before)
            self.credentials.values[self.account.id] = retained
            dialog.retry_credentials_button.invoke()
            self.wait_for_ui(
                lambda: not dialog.authorization_frame.winfo_manager(),
                "Valid saved credentials dismiss the recovery frame",
            )
            authorize.assert_not_called()
        self.assertEqual(dialog.variables["label"].get(), "Unsaved label")
        self.assertEqual(self.app.settings.to_dict(), before)
        self.assertTrue(dialog.winfo_exists())

    def test_selected_service_account_file_is_read_only_once_on_explicit_save(self):
        key = {
            "type": "service_account",
            "client_email": "synthetic@example.iam.gserviceaccount.com",
            "private_key": "synthetic",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        account = Account(
            "Workspace",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            mailboxes=[Mailbox("owner@example.org", ["INBOX"])],
        )
        self.app.save_account(AccountSubmission(account, {"google_service_account": key}, False))
        key_path = self.app.database_path.parent / "synthetic-key.json"
        key_path.write_text(json.dumps(key))
        before = self.app.settings.to_dict()
        dialog = AccountDialog(
            self.root,
            5,
            account,
            editor=self.app.account_editor(account.id),
            read_service_account=self.app.read_service_account,
        )
        self.addCleanup(dialog.destroy)
        with (
            patch.object(
                self.app, "_service_account_reader", wraps=self.app._service_account_reader
            ) as reader,
            patch.object(
                dialog, "_update_authorization", wraps=dialog._update_authorization
            ) as status,
            patch(
                "mailarchive.presentation.dialogs.filedialog.askopenfilename",
                return_value=str(key_path),
            ),
        ):
            dialog.service_account_button.invoke()
            initial = status.call_count
            self.wait_for_ui(
                lambda: status.call_count >= initial + 3,
                "The real status timer rendered several times after choosing the key file",
            )
            reader.assert_not_called()
            self.assertEqual(self.app.settings.to_dict(), before)
            dialog.save_button.invoke()
            reader.assert_called_once_with(str(key_path))
        self.assertFalse(dialog.winfo_exists())
        self.assertIsNotNone(dialog.result)
