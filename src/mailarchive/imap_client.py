from __future__ import annotations

import imaplib
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from mailarchive.models import Account, AuthMode, MailProvider


class MailboxError(RuntimeError):
    pass


@dataclass(slots=True)
class RemoteMessage:
    id: str
    raw: bytes


class ImapMailbox:
    def _connect(self, account: Account) -> imaplib.IMAP4:
        context = ssl.create_default_context()
        if account.use_ssl:
            return imaplib.IMAP4_SSL(account.host, account.port, ssl_context=context, timeout=30)
        client = imaplib.IMAP4(account.host, account.port, timeout=30)
        client.starttls(ssl_context=context)
        return client

    def fetch_messages(
        self,
        account: Account,
        password: str | None,
        should_fetch: Callable[[str, str], bool] | None = None,
        *,
        access_token: str | None = None,
    ) -> tuple[str, Iterator[RemoteMessage]]:
        if password is not None and access_token is not None:
            raise MailboxError("IMAP password and OAuth authentication cannot be used together.")
        if password is None and access_token is None:
            raise MailboxError("IMAP authentication credentials are missing.")
        if access_token is not None:
            if (
                account.provider != MailProvider.GENERIC_IMAP
                or account.auth_mode != AuthMode.OAUTH_USER
            ):
                raise MailboxError("The account is not configured for IMAP OAuth authentication.")
            try:
                account.validate()
            except ValueError as exc:
                raise MailboxError(str(exc)) from exc

        client: imaplib.IMAP4 | None = None
        try:
            client = self._connect(account)
            if access_token is None:
                client.login(account.username, password)
            else:
                self._authenticate_oauth(client, account.username, access_token)
            status, _ = client.select(account.folder, readonly=True)
            if status != "OK":
                raise MailboxError(f"Could not open mailbox '{account.folder}'.")
            _, validity_data = client.response("UIDVALIDITY")
            uid_validity = (
                validity_data[0].decode("ascii", errors="replace")
                if validity_data and validity_data[0]
                else "unknown"
            )
            status, uid_data = client.uid("search", None, "ALL")
            if status != "OK":
                raise MailboxError("Could not load the message list.")
            uids = uid_data[0].split() if uid_data and uid_data[0] else []
        except (OSError, ssl.SSLError, imaplib.IMAP4.error, UnicodeError, MailboxError) as exc:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            if isinstance(exc, MailboxError):
                raise
            raise MailboxError(str(exc)) from exc

        def iterator() -> Iterator[RemoteMessage]:
            try:
                for uid_bytes in uids:
                    uid = uid_bytes.decode("ascii")
                    if should_fetch is not None and not should_fetch(uid_validity, uid):
                        continue
                    status, response = client.uid("fetch", uid_bytes, "(BODY.PEEK[])")
                    if status != "OK":
                        raise MailboxError(
                            f"Could not load message {uid_bytes.decode(errors='replace')}."
                        )
                    raw = next(
                        (
                            item[1]
                            for item in response
                            if isinstance(item, tuple) and isinstance(item[1], bytes)
                        ),
                        None,
                    )
                    if raw is None:
                        raise MailboxError(
                            f"Message {uid_bytes.decode(errors='replace')} was empty."
                        )
                    yield RemoteMessage(id=uid, raw=raw)
            except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
                raise MailboxError(str(exc)) from exc
            finally:
                try:
                    client.close()
                except Exception:
                    pass
                try:
                    client.logout()
                except Exception:
                    pass

        return uid_validity, iterator()

    @staticmethod
    def _authenticate_oauth(client: imaplib.IMAP4, username: str, access_token: str) -> None:
        capabilities = {
            (
                capability.decode("ascii", errors="ignore")
                if isinstance(capability, bytes)
                else str(capability)
            ).upper()
            for capability in getattr(client, "capabilities", ())
        }
        if "AUTH=XOAUTH2" not in capabilities:
            raise MailboxError("The IMAP server does not support OAuth authentication (XOAUTH2).")

        try:
            payload = f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode()
        except UnicodeError as exc:
            raise MailboxError("Could not encode the IMAP OAuth identity.") from exc
        payload_sent = False

        def authentication_payload(_challenge: bytes) -> bytes:
            nonlocal payload_sent
            if payload_sent:
                return b""
            payload_sent = True
            return payload

        try:
            client.authenticate("XOAUTH2", authentication_payload)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxError(
                "IMAP OAuth authentication failed. Reauthorize the account and verify that IMAP "
                "access is enabled."
            ) from exc
