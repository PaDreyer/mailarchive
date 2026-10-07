"""Mailbox message identity is distinct from connection credentials and sync scope."""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from dataclasses import dataclass

from mailarchive.domain.configuration import Account, AuthMode, Mailbox, MailProvider


def imap_folder_wire_name(folder: str) -> str:
    """Encode names while preserving valid existing modified UTF-7 identities."""
    if folder.isascii():
        try:
            decoded = _decode_imap_folder_name(folder)
        except (ValueError, UnicodeError):
            pass
        else:
            if _encode_imap_folder_name(decoded) == folder:
                return folder
    return _encode_imap_folder_name(folder)


def _decode_imap_folder_name(folder: str) -> str:
    parts = folder.split("&")
    decoded = [parts[0]]
    for part in parts[1:]:
        encoded, separator, literal = part.partition("-")
        if not separator:
            raise ValueError("The modified UTF-7 shift is unterminated.")
        decoded.append(
            b64decode(encoded.replace(",", "/") + "=" * (-len(encoded) % 4), validate=True).decode(
                "utf-16-be"
            )
            if encoded
            else "&"
        )
        decoded.append(literal)
    return "".join(decoded)


def _encode_imap_folder_name(folder: str) -> str:
    result: list[str] = []
    shifted: list[str] = []

    def flush() -> None:
        if shifted:
            encoded = b64encode("".join(shifted).encode("utf-16-be")).decode("ascii")
            result.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            shifted.clear()

    for character in folder:
        if " " <= character <= "~":
            flush()
            result.append("&-" if character == "&" else character)
        else:
            shifted.append(character)
    flush()
    return "".join(result)


def is_imap_inbox(folder: str) -> bool:
    """Only ASCII case variants name IMAP's special INBOX mailbox."""
    return folder.isascii() and folder.upper() == "INBOX"


def folder_scope_key(provider: MailProvider, folder: str) -> str:
    if provider == MailProvider.GENERIC_IMAP:
        return "INBOX" if is_imap_inbox(folder) else imap_folder_wire_name(folder)
    return folder


def source_key(account: Account, mailbox: Mailbox) -> str:
    """Stable provider mailbox key, independent of account credentials."""
    address = mailbox.address.casefold()
    if account.provider == MailProvider.GENERIC_IMAP:
        if account.auth_mode == AuthMode.PASSWORD:
            return "imap-login:" + json.dumps(
                [account.host.casefold(), account.port, account.username, address]
            )
        return json.dumps([account.host.casefold(), account.port, address])
    return address


def legacy_source_key(account: Account, mailbox: Mailbox) -> str:
    """The pre-login-binding key, used only to recognize existing profile rows."""
    if account.provider == MailProvider.GENERIC_IMAP and account.auth_mode == AuthMode.PASSWORD:
        return json.dumps([account.host.casefold(), account.port, mailbox.address.casefold()])
    return source_key(account, mailbox)


def mailbox_namespace(account: Account, mailbox: Mailbox) -> str:
    address = mailbox.address.strip().casefold()
    if account.provider == MailProvider.GENERIC_IMAP:
        if account.auth_mode == AuthMode.PASSWORD:
            return "imap-login-mailbox:" + json.dumps(
                [account.host.casefold(), account.port, account.username, address],
                separators=(",", ":"),
            )
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
    folder = folder_scope_key(MailProvider.GENERIC_IMAP, target.folder)
    components: list[object] = [
        account.host.casefold(),
        account.port,
        target.mailbox.address.strip().casefold(),
        folder,
        uid_validity,
    ]
    prefix = "imap-v3:"
    if account.auth_mode == AuthMode.PASSWORD:
        prefix = "imap-v4:"
        components.insert(2, account.username)
    namespace = prefix + json.dumps(
        components,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return MessageScope(namespace, namespace)


def compatible_imap_scope(
    target: MailTarget,
    uid_validity: str,
    saved_namespace: str | None,
    *,
    retained_namespaces: frozenset[str] = frozenset(),
) -> MessageScope:
    """Retain a stored v3 cursor/receipt namespace for its frozen source identity.

    The profile source binding and protected credentials establish the exact login;
    an old namespace alone cannot establish it. New sources always use v4.
    """
    scope = imap_scope(target, uid_validity)
    if target.account.auth_mode != AuthMode.PASSWORD:
        return scope
    old = legacy_imap_scope(target, uid_validity).processing_namespace
    if saved_namespace == old or (saved_namespace is None and old in retained_namespaces):
        return MessageScope(old, old)
    return scope


def legacy_imap_scope(target: MailTarget, uid_validity: str) -> MessageScope:
    namespace = "imap-v3:" + json.dumps(
        [
            target.account.host.casefold(),
            target.account.port,
            target.mailbox.address.strip().casefold(),
            folder_scope_key(MailProvider.GENERIC_IMAP, target.folder),
            uid_validity,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return MessageScope(namespace, namespace)


def imap_namespace_matches(target: MailTarget, namespace: str) -> bool:
    """Validate both current and retained IMAP namespace formats against a snapshot."""
    try:
        prefix, payload = namespace.split(":", 1)
        parts = json.loads(payload)
        if prefix not in {"imap-v3", "imap-v4"} or not isinstance(parts, list):
            return False
        validity = parts[-1]
        if not isinstance(validity, str) or not validity.isascii() or not validity.isdigit():
            return False
        if not 1 <= int(validity) <= 4_294_967_295:
            return False
    except (ValueError, TypeError, IndexError):
        return False
    return compatible_imap_scope(target, validity, namespace).processing_namespace == namespace
