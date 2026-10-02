from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from mailarchive.application.account_credentials import load_credential_data
from mailarchive.application.credential_port import CredentialStore
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
    ) -> None:
        self.credential_store = credential_store
        self.mailbox = mailbox or ImapMailbox()
        self.oauth = oauth or OAuthManager(credential_store)

    def targets(self, account: Account, mailbox: Mailbox) -> list[MailTarget]:
        folders = mailbox.folders or self.list_folders(MailTarget(account, mailbox, ""))
        return [MailTarget(account, mailbox, folder, tuple(folders)) for folder in folders]

    def list_folders(self, target: MailTarget) -> list[str]:
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = str(
                load_credential_data(self.credential_store, account.id).get("password", "")
            )
            if not password:
                raise MailboxError("No password is stored. Edit the email account to add one.")
            return self.mailbox.list_folders(target, password=password)
        return self.mailbox.list_folders(
            target,
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
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            data = load_credential_data(self.credential_store, account.id)
            password = str(data.get("password", ""))
            if not password:
                raise MailboxError("No password is stored. Edit the email account to add one.")
            scope, messages = self.mailbox.fetch_messages(
                target,
                password,
                should_fetch,
                sync=sync,
            )
        elif account.auth_mode == AuthMode.OAUTH_USER:
            access_token = self.oauth.microsoft_access_token(account)
            scope, messages = self.mailbox.fetch_messages(
                target,
                None,
                should_fetch,
                access_token=access_token,
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
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = str(
                load_credential_data(self.credential_store, account.id).get("password", "")
            )
            if not password:
                raise MailboxError("No password is stored for this account.")
            return self.mailbox.fetch_messages(
                target,
                password,
                should_fetch,
                received_between=(start, end),
                range_sync=range_sync,
            )
        return self.mailbox.fetch_messages(
            target,
            None,
            should_fetch,
            access_token=self.oauth.microsoft_access_token(account),
            refresh_access_token=lambda: self.oauth.microsoft_access_token(
                account, force_refresh=True
            ),
            received_between=(start, end),
            range_sync=range_sync,
        )

    def fetch_message(
        self, target: MailTarget, remote_id: str, processing_namespace: str
    ) -> RemoteMessage | None:
        account = target.account
        if account.auth_mode == AuthMode.PASSWORD:
            password = str(
                load_credential_data(self.credential_store, account.id).get("password", "")
            )
            if not password:
                raise MailboxError("No password is stored for this account.")
            return self.mailbox.fetch_message(target, remote_id, processing_namespace, password)
        if account.auth_mode == AuthMode.OAUTH_USER:
            return self.mailbox.fetch_message(
                target,
                remote_id,
                processing_namespace,
                None,
                access_token=self.oauth.microsoft_access_token(account),
                refresh_access_token=lambda: self.oauth.microsoft_access_token(
                    account, force_refresh=True
                ),
            )
        raise MailboxError("Generic IMAP does not support application authentication.")
