"""Password store failures and legacy recovery through the production composition."""

import json
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    CredentialIdentityError,
    account_credential_lock,
    credential_binding,
)
from mailarchive.application.account_status import AccountAction, AuthorizationState
from mailarchive.application.credential_port import CredentialError
from mailarchive.domain.source_identity import MailTarget
from tests import test_credential_admission_regressions as credential_fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.helpers import sample_mail
from tests.test_imap_client import FakeImapConnection


@contextmanager
def password_profile(*, legacy=False, folders=("INBOX",)):
    fixture = credential_fixture.CredentialAdmissionRegressionTests()
    fixture.setUp()
    try:
        credentials = credential_fixture.NativeSecretStore(fixture)
        app = fixture.application(credentials)
        account = fixture.save_password(app, folders=list(folders))
        account.poll_minutes = 1
        app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)
        if not app._background.wait(THREAD_TIMEOUT):
            raise AssertionError("Initial account inspection did not settle")
        app.dispatch_callbacks()
        if legacy:
            credentials.set(account.id, "synthetic-password")
        source = app._context.execution.service.source_registry.get(account)
        profile = SimpleNamespace(
            app=app,
            account=account,
            credentials=credentials,
            source=source,
            service=app._context.execution.service,
            root=fixture.root,
            connections=[],
            network_failure=False,
            body_failure=False,
        )

        def connect(*args, **kwargs):
            if profile.network_failure:
                raise TimeoutError("Synthetic IMAP network timeout")
            connection = FakeImapConnection(raw_by_uid={b"77": sample_mail()})
            native = connection.uid

            def read(command, *arguments):
                if (
                    profile.body_failure
                    and command == "fetch"
                    and "BODY.PEEK[]" in str(arguments[-1])
                ):
                    raise ConnectionResetError("Synthetic MIME download interruption")
                return native(command, *arguments)

            connection.uid = read
            profile.connections.append(connection)
            return connection

        with ExitStack() as transports:
            transports.enter_context(patch.object(source.mailbox, "_connect", side_effect=connect))

            def reopen():
                assert profile.app.close(timeout=THREAD_TIMEOUT)
                profile.app = fixture.application(credentials)
                profile.account = profile.app.settings.accounts[0]
                profile.service = profile.app._context.execution.service
                profile.source = profile.service.source_registry.get(profile.account)
                transports.enter_context(
                    patch.object(profile.source.mailbox, "_connect", side_effect=connect)
                )

            profile.reopen = reopen
            try:
                yield profile
            finally:
                # Stop owned workers before removing the transport isolation.
                fixture.doCleanups()
    finally:
        fixture.doCleanups()


class PasswordCredentialRecoveryTests(unittest.TestCase):
    def wait_tasks(self, profile):
        self.assertTrue(profile.app._background.wait(THREAD_TIMEOUT))
        profile.app.dispatch_callbacks()

    def check(self, profile):
        observed = threading.Condition()
        progress = []

        def receive(item):
            with observed:
                progress.append(item)
                observed.notify_all()

        profile.app.set_observers(lambda event: None, receive)
        profile.app.start()
        check_id = profile.app.check_now()
        self.assertIsInstance(check_id, str)
        with observed:
            self.assertTrue(
                observed.wait_for(
                    lambda: any(
                        item.execution_id == check_id and item.state and not item.state.active
                        for item in progress
                    ),
                    THREAD_TIMEOUT,
                )
            )
        self.assertTrue(profile.app._context.execution.is_idle())
        return next(
            item
            for item in progress
            if item.execution_id == check_id and item.state and not item.state.active
        )

    def assert_unavailable(self, profile):
        status = profile.app.account_status(profile.account.id)
        self.assertEqual(status.authorization.state, AuthorizationState.UNAVAILABLE)
        self.assertFalse(status.allows(AccountAction.CHECK_MAIL))
        self.assertFalse(status.allows(AccountAction.RETRY_REMOTE))

    def refresh(self, profile):
        profile.credentials.locked = False
        before = dict(profile.credentials.values)
        profile.app.refresh_account_authorization(profile.account.id)
        self.wait_tasks(profile)
        self.assertEqual(
            profile.credentials.values, before, "Inspection must not rewrite credentials"
        )
        status = profile.app.account_status(profile.account.id)
        self.assertEqual(status.authorization.state, AuthorizationState.NOT_REQUIRED)
        self.assertTrue(status.allows(AccountAction.CHECK_MAIL))

    def test_real_check_publishes_failure_and_reinspection_resumes_without_another_unlock(self):
        with password_profile() as profile:
            profile.credentials.locked = True
            terminal = self.check(profile)
            self.assertEqual(terminal.state.value, "failed")
            self.assert_unavailable(profile)
            self.assertEqual(profile.credentials.unlocks, 1)
            self.assertEqual(profile.connections, [])
            self.assertIsNone(profile.app.check_now())
            self.assertEqual(profile.credentials.unlocks, 1)
            self.refresh(profile)
            self.assertEqual(self.check(profile).state.value, "completed")
            self.assertEqual(len(list((profile.root / "archive").glob("*.eml"))), 1)

    def test_folder_listing_uses_the_same_status_publication_and_local_recovery(self):
        with password_profile() as profile:
            profile.credentials.locked = True
            with self.assertRaises(CredentialError):
                profile.source.list_folders(
                    MailTarget(profile.account, profile.account.mailboxes[0], "")
                )
            self.assert_unavailable(profile)
            self.assertEqual(profile.connections, [])
            self.refresh(profile)
            self.assertEqual(profile.credentials.unlocks, 1)

    def test_automatic_polls_do_not_prompt_again_after_a_store_failure(self):
        clock = [0.0]
        with (
            patch("mailarchive.application.execution.time.monotonic", lambda: clock[0]),
            password_profile() as profile,
        ):
            profile.app.start()
            self.wait_tasks(profile)
            profile.credentials.locked = True
            self.assertEqual(self.check(profile).state.value, "failed")
            self.assert_unavailable(profile)
            coordinator = profile.app._context.execution
            native = coordinator._poll
            observed = threading.Condition()
            polls = []

            def poll(*args, **kwargs):
                result = native(*args, **kwargs)
                with observed:
                    polls.append(clock[0])
                    observed.notify_all()
                return result

            def wait_poll(count):
                with observed:
                    self.assertTrue(observed.wait_for(lambda: len(polls) >= count, THREAD_TIMEOUT))
                deadline = time.perf_counter() + THREAD_TIMEOUT
                while not coordinator.is_idle() and time.perf_counter() < deadline:
                    threading.Event().wait(0.002)
                self.assertTrue(coordinator.is_idle())

            with patch.object(coordinator, "_poll", side_effect=poll):
                profile.app.set_automatic_monitoring_paused(False)
                wait_poll(1)
                for count, seconds in enumerate((61, 122), start=2):
                    clock[0] = seconds
                    coordinator.settings_changed(profile.app.settings)
                    wait_poll(count)
            self.assertEqual(polls, [0, 61, 122])
            self.assertEqual(profile.credentials.unlocks, 1)
            self.assertEqual(profile.connections, [])

    def test_manual_range_remains_retryable_and_keeps_its_frozen_snapshot(self):
        with password_profile() as profile:
            operation = profile.service.prepare_range_operation(
                profile.app.settings,
                {profile.account.mailboxes[0].id},
                rule_id=profile.app.settings.rules[0].id,
            )
            profile.credentials.locked = True
            result = profile.service.run_range_operation(operation)[0]
            self.assertEqual((result.archived, result.failed), (0, 1))
            self.assert_unavailable(profile)
            run = profile.service.operations.manual_run_for_source(
                operation, profile.account.mailboxes[0].id
            )
            saved = profile.service.operations.run_settings_snapshot(run["id"])
            self.refresh(profile)
            result = profile.service.run_range_operation(operation)[0]
            self.assertEqual((result.archived, result.failed), (1, 0))
            self.assertEqual(
                profile.service.operations.manual_operation(operation)["status"], "completed"
            )
            self.assertEqual(profile.service.operations.run_settings_snapshot(run["id"]), saved)

    def test_saved_remote_retry_reports_store_failure_before_provider_access(self):
        with password_profile() as profile:
            profile.body_failure = True
            self.assertEqual(profile.service.run_once(profile.app.settings)[0].failed, 1)
            intake = profile.service.discovery.pending_automatic_intakes(due_only=False)[0]
            saved = profile.service.operations.run_settings_snapshot(intake["run_id"])
            connections = len(profile.connections)
            profile.credentials.locked = True
            result = profile.service.run_once(
                profile.app.settings, account_ids=set(), force_retry=True
            )[0]
            self.assertEqual((result.archived, result.failed), (0, 1))
            self.assert_unavailable(profile)
            self.assertEqual(len(profile.connections), connections)
            self.assertEqual(
                profile.service.operations.run_settings_snapshot(intake["run_id"]), saved
            )
            self.refresh(profile)
            profile.body_failure = False
            result = profile.service.run_once(
                profile.app.settings, account_ids=set(), force_retry=True
            )[0]
            self.assertEqual((result.archived, result.failed), (1, 0))
            self.assertEqual(
                profile.service.discovery.pending_automatic_intakes(due_only=False), []
            )

    def test_network_errors_do_not_publish_a_protected_record_failure(self):
        with password_profile() as profile:
            profile.network_failure = True
            self.assertEqual(self.check(profile).state.value, "failed")
            status = profile.app.account_status(profile.account.id)
            self.assertEqual(status.authorization.state, AuthorizationState.NOT_REQUIRED)
            self.assertTrue(status.allows(AccountAction.CHECK_MAIL))
            self.assertEqual(profile.credentials.unlocks, 0)

    def test_record_failure_is_published_while_the_account_credential_lock_is_owned(self):
        with password_profile() as profile:
            native = profile.source.oauth.on_credentials_unavailable
            acquired = []

            def publish(account, detail):
                lock = account_credential_lock(account.id)

                def contend():
                    success = lock.acquire(blocking=False)
                    acquired.append(success)
                    if success:
                        lock.release()

                contender = threading.Thread(target=contend)
                contender.start()
                contender.join(THREAD_TIMEOUT)
                self.assertFalse(contender.is_alive())
                native(account, detail)

            profile.credentials.locked = True
            with patch.object(
                profile.source.oauth, "on_credentials_unavailable", side_effect=publish
            ):
                with self.assertRaises(CredentialError):
                    profile.source.list_folders(
                        MailTarget(profile.account, profile.account.mailboxes[0], "")
                    )
            self.assertEqual(acquired, [False])
            self.assert_unavailable(profile)

    def test_legacy_reinspection_does_not_bind_or_rewrite_the_record(self):
        with password_profile(legacy=True) as profile:
            profile.credentials.locked = True
            self.assertEqual(self.check(profile).state.value, "failed")
            self.assert_unavailable(profile)
            self.refresh(profile)
            self.assertEqual(profile.credentials.values[profile.account.id], "synthetic-password")
            self.assertEqual(profile.connections, [])
            self.assertEqual(self.check(profile).state.value, "completed")
            record = json.loads(profile.credentials.values[profile.account.id])
            self.assertEqual(
                record["credential_binding"], list(credential_binding(profile.account))
            )

    def test_legacy_inspection_rejects_a_different_frozen_identity_without_publication(self):
        with password_profile(legacy=True) as profile:
            snapshot = dict(profile.credentials.values)
            frozen = deepcopy(profile.account)
            frozen.username = "another-owner@example.org"
            with self.assertRaises(CredentialIdentityError):
                profile.source.oauth.authorization_status(frozen)
            self.assertEqual(profile.credentials.values, snapshot)
            self.assertEqual(
                profile.app.account_status(profile.account.id).authorization.state,
                AuthorizationState.NOT_REQUIRED,
            )

    def test_legacy_recovery_survives_a_real_profile_restart(self):
        with password_profile(legacy=True) as profile:
            profile.credentials.locked = True
            self.assertEqual(self.check(profile).state.value, "failed")
            self.assert_unavailable(profile)
            profile.reopen()
            self.assertEqual(self.check(profile).state.value, "failed")
            self.assert_unavailable(profile)
            self.refresh(profile)
            self.assertEqual(profile.credentials.values[profile.account.id], "synthetic-password")
            self.assertEqual(self.check(profile).state.value, "completed")
            self.assertEqual(len(list((profile.root / "archive").glob("*.eml"))), 1)

    def test_unbound_and_v1_bound_json_are_inspected_without_migration(self):
        for binding in (None, 1):
            with self.subTest(binding=binding), password_profile() as profile:
                record = {"format_version": 1, "password": "synthetic-password"}
                if binding is not None:
                    record["credential_binding"] = list(credential_binding(profile.account))
                    record["credential_binding_version"] = binding
                serialized = json.dumps(record)
                profile.credentials.set(profile.account.id, serialized)
                profile.credentials.locked = True
                self.assertEqual(self.check(profile).state.value, "failed")
                self.refresh(profile)
                self.assertEqual(profile.credentials.values[profile.account.id], serialized)
                self.assertEqual(self.check(profile).state.value, "completed")
