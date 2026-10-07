"""Keep unfinished remote intake attached to its frozen account and selection."""

from mailarchive.application.account_status import authorization_binding
from mailarchive.domain.configuration import Account, Mailbox, MailProvider, Settings
from mailarchive.domain.source_identity import folder_scope_key


class IntakeOwnerDeferred(RuntimeError):
    """An unfinished owner must settle before another discovery can claim the ID."""


def automatic_intake_owner_matches(saved: Settings, account: Account, mailbox: Mailbox) -> bool:
    owner = next(
        (
            (candidate, source)
            for candidate in saved.accounts
            for source in candidate.mailboxes
            if source.id == mailbox.id
        ),
        None,
    )
    if owner is None:
        return False
    old_account, old_mailbox = owner
    return (
        old_account.id == account.id
        and authorization_binding(old_account) == authorization_binding(account)
        and (
            account.provider == MailProvider.GENERIC_IMAP
            or {folder_scope_key(old_account.provider, folder) for folder in old_mailbox.folders}
            == {folder_scope_key(account.provider, folder) for folder in mailbox.folders}
        )
    )
