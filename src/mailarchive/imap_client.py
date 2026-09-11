from __future__ import annotations

import imaplib
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from mailarchive.models import Account


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
        password: str,
        should_fetch: Callable[[str, str], bool] | None = None,
    ) -> tuple[str, Iterator[RemoteMessage]]:
        client: imaplib.IMAP4 | None = None
        try:
            client = self._connect(account)
            client.login(account.username, password)
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
                        (item[1] for item in response if isinstance(item, tuple) and isinstance(item[1], bytes)),
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
