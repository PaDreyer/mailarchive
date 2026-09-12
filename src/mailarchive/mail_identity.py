"""Mailbox message identity is distinct from connection credentials and sync scope."""

from __future__ import annotations

import json
from dataclasses import dataclass

from mailarchive.models import Account, Mailbox, MailProvider


def mailbox_namespace(account: Account, mailbox: Mailbox) -> str:
    address = mailbox.address.strip().casefold()
    if account.provider == MailProvider.GENERIC_IMAP:
        return "imap-mailbox:" + json.dumps(
            [account.host.casefold(), account.port, address],
            separators=(",", ":"),
        )
    return f"{account.provider.value}-mailbox:{address}"


@dataclass(frozen=True, slots=True)
class MailTarget:
    account: Account
    mailbox: Mailbox
    folder: str
    selected_folders: tuple[str, ...] = ()

    @property
    def mailbox_namespace(self) -> str:
        return mailbox_namespace(self.account, self.mailbox)

    @property
    def label(self) -> str:
        folder = f" / {self.folder}" if self.folder else ""
        return f"{self.account.label}: {self.mailbox.address}{folder}"


@dataclass(frozen=True, slots=True)
class MessageScope:
    processing_namespace: str
    synchronization_namespace: str


def api_scope(target: MailTarget) -> MessageScope:
    processing = target.mailbox_namespace
    sync = processing
    if target.account.provider == MailProvider.MICROSOFT_GRAPH:
        sync += ":folder:" + json.dumps(target.folder, ensure_ascii=True)
    return MessageScope(processing, sync)


def imap_scope(target: MailTarget, uid_validity: str) -> MessageScope:
    account = target.account
    folder = "INBOX" if target.folder.upper() == "INBOX" else target.folder
    namespace = "imap-v3:" + json.dumps(
        [
            account.host.casefold(),
            account.port,
            target.mailbox.address.strip().casefold(),
            folder,
            uid_validity,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return MessageScope(namespace, namespace)
