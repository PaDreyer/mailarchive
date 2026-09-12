from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from mailarchive.mail_identity import MailTarget, MessageScope, imap_scope
from mailarchive.models import Account, AuthMode, MailProvider
from mailarchive.synchronization import SyncSession


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

    def list_folders(
        self, target: MailTarget, *, password: str | None = None, access_token: str | None = None
    ) -> list[str]:
        client = self._connect(target.account)
        try:
            if access_token is not None:
                target.account.validate()
                self._authenticate_oauth(client, target.mailbox.address, access_token)
            elif password is not None:
                client.login(target.account.username, password)
            else:
                raise MailboxError("IMAP authentication credentials are missing.")
            status, lines = client.list('""', '"*"')
            if status != "OK":
                raise MailboxError("Could not discover mailbox folders.")
            folders = []
            for line in lines or []:
                literal = None
                if isinstance(line, tuple):
                    line, literal = line
                if not isinstance(line, bytes):
                    raise MailboxError("The IMAP server returned an invalid folder list.")
                match = re.fullmatch(rb'\(([^)]*)\) (?:NIL|"(?:[^"\\]|\\.)*") (.+)', line)
                if not match:
                    raise MailboxError("The IMAP server returned an invalid folder entry.")
                if b"\\noselect" in match[1].lower().split():
                    continue
                name = literal if literal is not None else match[2]
                if literal is None and name.startswith(b'"') and name.endswith(b'"'):
                    name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
                # Preserve the server's modified UTF-7 wire name. imaplib uses ASCII.
                folders.append(name.decode("ascii"))
            return list(dict.fromkeys(folders))
        except (OSError, imaplib.IMAP4.error, UnicodeError) as exc:
            raise MailboxError(str(exc)) from exc
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def fetch_messages(
        self,
        target: MailTarget,
        password: str | None,
        should_fetch: Callable[[MessageScope, str], bool] | None = None,
        *,
        access_token: str | None = None,
        sync: SyncSession | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        account = target.account
        self._validate_authentication(account, password, access_token)

        client: imaplib.IMAP4 | None = None
        try:
            client = self._connect(account)
            if access_token is None:
                client.login(account.username, password)
            else:
                self._authenticate_oauth(client, target.mailbox.address, access_token)
            uid_validity = self._select_folder(client, target.folder)
            scope = imap_scope(target, uid_validity)
            uids, next_uid = self._message_uids(client, scope, uid_validity, sync)
        except Exception as exc:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            if isinstance(
                exc, (OSError, ssl.SSLError, imaplib.IMAP4.error, UnicodeError, ValueError)
            ):
                raise MailboxError(str(exc)) from exc
            raise

        def iterator() -> Iterator[RemoteMessage]:
            try:
                for uid_bytes in uids:
                    uid = uid_bytes.decode("ascii")
                    if should_fetch is not None and not should_fetch(scope, uid):
                        continue
                    yield self._download_message(client, uid_bytes, uid_validity)
                if sync is not None:
                    sync.next_cursor = str(next_uid)
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

        return scope, iterator()

    @staticmethod
    def _validate_authentication(
        account: Account, password: str | None, access_token: str | None
    ) -> None:
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

    def _select_folder(self, client: imaplib.IMAP4, folder: str) -> str:
        for _ in range(2):
            status, _ = client.select(self._quoted_folder(folder), readonly=True)
            if status != "OK":
                raise MailboxError(f"Could not open mailbox folder '{folder}'.")
            _, validity_data = client.response("UIDVALIDITY")
            if validity_data is not None and not isinstance(validity_data, list):
                raise MailboxError("The IMAP server returned an invalid UIDVALIDITY response.")
            if validity_data and validity_data != [None]:
                if len(validity_data) != 1 or not isinstance(validity_data[0], bytes):
                    raise MailboxError("The IMAP server returned an invalid UIDVALIDITY.")
                return str(self._unsigned_number(validity_data[0], "UIDVALIDITY"))
        raise MailboxError(
            "The IMAP server did not return the required UIDVALIDITY after "
            "reopening the folder. Synchronization was stopped safely."
        )

    def _message_uids(
        self,
        client: imaplib.IMAP4,
        scope: MessageScope,
        uid_validity: str,
        sync: SyncSession | None,
    ) -> tuple[list[bytes], int]:
        cursor = sync.cursor_for(scope.synchronization_namespace) if sync is not None else None
        last_uid = (
            self._unsigned_number(cursor, "stored synchronization UID", allow_zero=True)
            if cursor is not None
            else 0
        )
        criterion = f"UID {min(last_uid + 1, 4294967295)}:*" if cursor is not None else "ALL"
        status, uid_data = client.uid("search", None, criterion)
        if status != "OK":
            raise MailboxError("Could not load the message list.")
        self._check_uidvalidity(client, uid_validity)
        # IMAP ranges are inclusive in either direction. n:* can return the last
        # message even when its UID is smaller than n.
        uids = [uid for uid in self._search_uids(uid_data) if int(uid) > last_uid]
        next_uid = max((int(uid) for uid in uids), default=last_uid)
        if sync is not None:
            recheck_ids = sync.recheck_ids_for(scope.processing_namespace)
            if recheck_ids:
                existing = self._recheck_uids(client, recheck_ids, uid_validity)
                sync.discarded_ids.update(recheck_ids - {uid.decode("ascii") for uid in existing})
                uids = sorted(set(uids) | existing, key=int)
            for uid in uids:
                sync.mark_present(uid.decode("ascii"))
        return uids, next_uid

    def _recheck_uids(
        self, client: imaplib.IMAP4, recheck_ids: set[str], uid_validity: str
    ) -> set[bytes]:
        requested_ids = sorted(
            recheck_ids, key=lambda uid: self._unsigned_number(uid, "stored message UID")
        )
        existing: set[bytes] = set()
        for start in range(0, len(requested_ids), 500):
            requested = ",".join(requested_ids[start : start + 500])
            status, uid_data = client.uid("search", None, f"UID {requested}")
            if status != "OK":
                raise MailboxError("Could not load messages requiring another check.")
            self._check_uidvalidity(client, uid_validity)
            existing.update(self._search_uids(uid_data))
        return {uid for uid in existing if uid.decode("ascii") in recheck_ids}

    def _download_message(
        self, client: imaplib.IMAP4, uid: bytes, uid_validity: str
    ) -> RemoteMessage:
        status, response = client.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise MailboxError(f"Could not load message {uid.decode(errors='replace')}.")
        self._check_uidvalidity(client, uid_validity)
        raw = next(
            (
                item[1]
                for item in response
                if isinstance(item, tuple) and isinstance(item[1], bytes)
            ),
            None,
        )
        if raw is None:
            raise MailboxError(f"Message {uid.decode(errors='replace')} was empty.")
        return RemoteMessage(id=uid.decode("ascii"), raw=raw)

    @classmethod
    def _check_uidvalidity(cls, client: imaplib.IMAP4, expected: str) -> None:
        # response() consumes imaplib's cached response code; this sends no command.
        _, data = client.response("UIDVALIDITY")
        if data is not None and not isinstance(data, list):
            raise MailboxError("The IMAP server returned an invalid UIDVALIDITY response.")
        if data is None or data == [None] or data == []:
            return
        if (
            len(data) != 1
            or not isinstance(data[0], bytes)
            or str(cls._unsigned_number(data[0], "UIDVALIDITY")) != expected
        ):
            raise MailboxError("The IMAP UIDVALIDITY changed while the folder was open.")

    @staticmethod
    def _unsigned_number(value: bytes | str, name: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeError as exc:
                raise MailboxError(f"The IMAP {name} is invalid.") from exc
        if (
            not isinstance(value, str)
            or re.fullmatch(r"0|[1-9][0-9]*", value) is None
            or len(value) > 10
            or not (0 if allow_zero else 1) <= int(value) <= 4294967295
        ):
            raise MailboxError(f"The IMAP {name} is invalid.")
        return int(value)

    @classmethod
    def _search_uids(cls, data: list[bytes]) -> list[bytes]:
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], bytes):
            raise MailboxError("The IMAP server returned an invalid UID search response.")
        uids = data[0].split()
        for uid in uids:
            cls._unsigned_number(uid, "message UID")
        return list(dict.fromkeys(uids))

    @staticmethod
    def _quoted_folder(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

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
