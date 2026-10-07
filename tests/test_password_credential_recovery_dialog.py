"""Real password store failures expose local recovery without saving the draft."""

import tkinter as tk

from mailarchive.presentation.dialogs import AccountDialog
from tests import test_password_credential_recovery as credential_fixture
from tests.tk_test_case import TkTestCase


class PasswordCredentialRecoveryDialogTests(TkTestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def exercise_recovery(self, *, legacy):
        with credential_fixture.password_profile(legacy=legacy) as profile:
            before = profile.app.settings.to_dict()
            dialog = AccountDialog(
                self.root,
                5,
                profile.account,
                editor=profile.app.account_editor(profile.account.id),
            )
            try:
                dialog.variables["label"].set("Unsaved label")
                profile.credentials.locked = True
                terminal = credential_fixture.PasswordCredentialRecoveryTests.check(self, profile)
                self.assertEqual(terminal.state.value, "failed")
                self.wait_for_ui(
                    lambda: bool(dialog.retry_credentials_button.winfo_manager()),
                    "A real provider credential failure exposes local reinspection",
                )
                self.assertEqual(dialog.authorization_frame.cget("text"), "Saved credentials")
                self.assertFalse(dialog.authorize_button.winfo_manager())
                self.assertFalse(dialog.cancel_authorization_button.winfo_manager())
                self.assertEqual(profile.credentials.unlocks, 1)
                self.assertIsNone(profile.app.check_now())
                self.assertEqual(profile.credentials.unlocks, 1)
                profile.credentials.locked = False
                credential_snapshot = dict(profile.credentials.values)
                dialog.retry_credentials_button.invoke()
                self.wait_for_ui(
                    lambda: not dialog.authorization_frame.winfo_manager(),
                    "Unlocking the saved record removes the recovery gate",
                )
                self.assertEqual(profile.credentials.values, credential_snapshot)
                self.assertEqual(profile.app.settings.to_dict(), before)
                self.assertEqual(dialog.variables["label"].get(), "Unsaved label")
                self.assertTrue(dialog.winfo_exists())
                terminal = credential_fixture.PasswordCredentialRecoveryTests.check(self, profile)
                self.assertEqual(terminal.state.value, "completed")
                self.assertEqual(len(list((profile.root / "archive").glob("*.eml"))), 1)
            finally:
                dialog.destroy()

    def test_bound_password_failure_and_reinspection_preserve_the_unsaved_draft(self):
        self.exercise_recovery(legacy=False)

    def test_legacy_password_failure_and_reinspection_preserve_the_unsaved_draft(self):
        self.exercise_recovery(legacy=True)
