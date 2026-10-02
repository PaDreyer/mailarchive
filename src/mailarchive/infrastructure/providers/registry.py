from __future__ import annotations

from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation
from mailarchive.application.credential_port import CredentialStore
from mailarchive.application.source_port import MailboxError, MessageSource
from mailarchive.domain.configuration import Account, Mailbox, MailProvider
from mailarchive.domain.source_identity import MailTarget
from mailarchive.infrastructure.oauth import OAuthManager
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.http import HttpClient
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox


class MessageSourceRegistry:
    def __init__(
        self,
        credential_store: CredentialStore,
        imap_mailbox: ImapMailbox | None = None,
        http: HttpClient | None = None,
    ) -> None:
        oauth = OAuthManager(credential_store)
        self.sources: dict[MailProvider, MessageSource] = {
            MailProvider.GENERIC_IMAP: ImapMessageSource(
                credential_store,
                imap_mailbox,
                oauth,
            ),
            MailProvider.GMAIL_API: GmailMessageSource(oauth, http),
            MailProvider.MICROSOFT_GRAPH: MicrosoftGraphMessageSource(oauth, http),
        }

    def get(self, account: Account) -> MessageSource:
        try:
            return self.sources[account.provider]
        except KeyError as exc:
            raise MailboxError(f"Unsupported mail provider: {account.provider}") from exc

    def targets(
        self, account: Account, mailbox: Mailbox, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[MailTarget]:
        return self.get(account).targets(account, mailbox, cancellation=cancellation)
