from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime

from mailarchive.application.account_credentials import (
    account_credential_lock,
    load_account_credential_data,
)
from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation
from mailarchive.application.credential_port import CredentialError, CredentialStore
from mailarchive.application.source_port import MailboxError, MessageFilter, RemoteMessage
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import Account, AuthMode, Mailbox
from mailarchive.domain.source_identity import MailTarget, MessageScope
from mailarchive.infrastructure.oauth import OAuthManager
from mailarchive.infrastructure.providers.imap_client import ImapMailbox


class ImapMessageSource:
    def __init__(
        self,
        credential_store: CredentialStore,
        mailbox: ImapMailbox | None = None,
        oauth: OAuthManager | None = None,
        *,
        live_account: Callable[[str], Account | None] | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.mailbox = mailbox or ImapMailbox()
        self.oauth = oauth or OAuthManager(credential_store, live_account=live_account)
        self.live_account = live_account

    def _password(self, account: Account) -> str:
        with account_credential_lock(account.id):
            try:
                data = load_account_credential_data(
                    self.credential_store, account, self.live_account
                )
            except CredentialError as exc:
                self.oauth.on_credentials_unavailable(account, str(exc))
                raise
        password = str(data.get("password", ""))
        if not password:
            raise MailboxError("No password is stored. Edit the email account to add one.")
        return password

    def targets(
        self, account: Account, mailbox: Mailbox, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[MailTarget]:
        folders = mailbox.folders or self.list_folders(
            MailTarget(account, mailbox, ""), cancellation=cancellation
        )
        return [MailTarget(account, mailbox, folder, tuple(folders)) for folder in folders]

    def list_folders(
        self, target: MailTarget, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[str]:
        cancellation.checkpoint()
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = self._password(account)
            return self.mailbox.list_folders(target, password=password, cancellation=cancellation)
        return self.mailbox.list_folders(
            target,
            cancellation=cancellation,
            access_token=self.oauth.microsoft_access_token(account),
            refresh_access_token=lambda: self.oauth.microsoft_access_token(
                account, force_refresh=True
            ),
        )

    def fetch_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        *,
        sync: SyncSession | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        cancellation.checkpoint()
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = self._password(account)
            scope, messages = self.mailbox.fetch_messages(
                target,
                password,
                should_fetch,
                sync=sync,
                cancellation=cancellation,
            )
        elif account.auth_mode == AuthMode.OAUTH_USER:
            access_token = self.oauth.microsoft_access_token(account)
            scope, messages = self.mailbox.fetch_messages(
                target,
                None,
                should_fetch,
                access_token=access_token,
                cancellation=cancellation,
                refresh_access_token=lambda: self.oauth.microsoft_access_token(
                    account, force_refresh=True
                ),
                sync=sync,
            )
        else:
            raise MailboxError("Generic IMAP does not support application authentication.")
        return scope, messages

    def search_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        start: datetime | None,
        end: datetime | None,
        *,
        range_sync: RangePagination | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        cancellation.checkpoint()
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = self._password(account)
            return self.mailbox.fetch_messages(
                target,
                password,
                should_fetch,
                received_between=(start, end),
                range_sync=range_sync,
                cancellation=cancellation,
            )
        return self.mailbox.fetch_messages(
            target,
            None,
            should_fetch,
            cancellation=cancellation,
            access_token=self.oauth.microsoft_access_token(account),
            refresh_access_token=lambda: self.oauth.microsoft_access_token(
                account, force_refresh=True
            ),
            received_between=(start, end),
            range_sync=range_sync,
        )

    def fetch_message(
        self,
        target: MailTarget,
        remote_id: str,
        processing_namespace: str,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> RemoteMessage | None:
        cancellation.checkpoint()
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = self._password(account)
            return self.mailbox.fetch_message(
                target, remote_id, processing_namespace, password, cancellation=cancellation
            )
        if account.auth_mode == AuthMode.OAUTH_USER:
            return self.mailbox.fetch_message(
                target,
                remote_id,
                processing_namespace,
                None,
                cancellation=cancellation,
                access_token=self.oauth.microsoft_access_token(account),
                refresh_access_token=lambda: self.oauth.microsoft_access_token(
                    account, force_refresh=True
                ),
            )
        raise MailboxError("Generic IMAP does not support application authentication.")
