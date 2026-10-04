from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation
from mailarchive.application.intake_limits import MESSAGE_CHUNK_BYTES
from mailarchive.application.source_port import (
    MailboxError,
    RemoteMessage,
    RemoteMessageError,
    RemoteMessageUnavailable,
)
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import Account, AuthMode, MailHeaders, MailProvider
from mailarchive.domain.mail_parser import parse_headers
from mailarchive.domain.source_identity import MailTarget, MessageScope, imap_scope

_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_IMAP_MONTH_NUMBERS = {month.lower(): number for number, month in enumerate(_IMAP_MONTHS, 1)}
IMAP_METADATA_BATCH_SIZE = 100
IMAP_HEADER_BYTES = 64 * 1024
_Metadata = tuple[datetime, int, MailHeaders | None]
_MetadataResult = _Metadata | RemoteMessageError | RemoteMessageUnavailable


def _imap_search_date(value: datetime) -> str:
    """Format an IMAP calendar date without consulting the process locale."""
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year:04d}"


def _parse_internaldate(value: bytes) -> datetime:
    """Parse IMAP's fixed English INTERNALDATE grammar without locale state."""
    try:
        text = value.decode("ascii")
    except UnicodeError as exc:
        raise ValueError("INTERNALDATE is not ASCII.") from exc
    match = re.fullmatch(
        r"(?P<day>[0-9]{1,2})-(?P<month>[A-Za-z]{3})-(?P<year>[0-9]{4}) "
        r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2}) "
        r"(?P<sign>[+-])(?P<zone_hour>[0-9]{2})(?P<zone_minute>[0-9]{2})",
        text,
    )
    if match is None:
        raise ValueError("INTERNALDATE has an invalid shape.")
    month = _IMAP_MONTH_NUMBERS.get(match["month"].lower())
    if month is None:
        raise ValueError("INTERNALDATE has an invalid month.")
    zone_hour = int(match["zone_hour"])
    zone_minute = int(match["zone_minute"])
    if zone_hour > 23 or zone_minute > 59:
        raise ValueError("INTERNALDATE has an invalid timezone offset.")
    zone_delta = timedelta(hours=zone_hour, minutes=zone_minute)
    if match["sign"] == "-":
        zone_delta = -zone_delta
    return datetime(
        int(match["year"]),
        month,
        int(match["day"]),
        int(match["hour"]),
        int(match["minute"]),
        int(match["second"]),
        tzinfo=timezone(zone_delta),
    )


class _AccessTokenExpired(MailboxError):
    pass


_ReadResult = TypeVar("_ReadResult")


class ImapMailbox:
    def _connect(
        self, account: Account, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> imaplib.IMAP4:
        cancellation.checkpoint()
        context = ssl.create_default_context()
        if account.use_ssl:
            return imaplib.IMAP4_SSL(account.host, account.port, ssl_context=context, timeout=30)
        client = imaplib.IMAP4(account.host, account.port, timeout=30)
        try:
            cancellation.checkpoint()
            client.starttls(ssl_context=context)
            return client
        except Exception:
            client.shutdown()
            raise

    def list_folders(
        self,
        target: MailTarget,
        *,
        password: str | None = None,
        access_token: str | None = None,
        refresh_access_token: Callable[[], str] | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> list[str]:
        self._validate_authentication(target.account, password, access_token)
        session = _ImapReadSession(
            self, target, password, access_token, refresh_access_token, cancellation
        )
        try:
            lines = session.read(self._folder_lines)
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
            session.close()

    def _folder_lines(self, client: imaplib.IMAP4) -> list:
        status, lines = client.list('""', '"*"')
        self._require_ok(status, lines, "Could not discover mailbox folders.")
        return lines

    def fetch_messages(
        self,
        target: MailTarget,
        password: str | None,
        should_fetch: Callable[[MessageScope, str], bool] | None = None,
        *,
        access_token: str | None = None,
        refresh_access_token: Callable[[], str] | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
        sync: SyncSession | None = None,
        received_between: tuple[datetime | None, datetime | None] | None = None,
        range_sync: RangePagination | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        account = target.account
        self._validate_authentication(account, password, access_token)

        session = _ImapReadSession(
            self, target, password, access_token, refresh_access_token, cancellation
        )
        try:
            uid_validity = session.read(
                lambda client: self._select_folder(client, target.folder, cancellation=cancellation)
            )
            session.uid_validity = uid_validity
            scope = imap_scope(target, uid_validity)
            range_after = (
                range_sync.start(scope.processing_namespace) if range_sync is not None else None
            )
            uids, next_uid = session.read(
                lambda client: self._message_uids(
                    client,
                    scope,
                    uid_validity,
                    sync,
                    received_between,
                    range_after,
                    cancellation=cancellation,
                )
            )
        except Exception as exc:
            session.close()
            if isinstance(
                exc, (OSError, ssl.SSLError, imaplib.IMAP4.error, UnicodeError, ValueError)
            ):
                raise MailboxError(str(exc)) from exc
            raise

        def iterator() -> Iterator[RemoteMessage]:
            try:
                metadata: dict[bytes, _MetadataResult] = {}
                for index, uid_bytes in enumerate(uids):
                    cancellation.checkpoint()
                    uid = uid_bytes.decode("ascii")
                    if should_fetch is not None and not should_fetch(scope, uid):
                        if range_sync is not None:
                            range_sync.advance(uid)
                        continue
                    try:
                        if uid_bytes not in metadata:
                            metadata = session.read(
                                lambda client, index=index: self._metadata_batch(
                                    client,
                                    uids[index : index + IMAP_METADATA_BATCH_SIZE],
                                    uid_validity,
                                    cancellation=cancellation,
                                )
                            )
                        item = metadata[uid_bytes]
                        if isinstance(item, (RemoteMessageError, RemoteMessageUnavailable)):
                            raise item
                        received, raw_size, headers = item
                    except (RemoteMessageError, RemoteMessageUnavailable) as exc:
                        yield RemoteMessage(id=uid, error=exc)
                        if range_sync is not None:
                            range_sync.advance(uid)
                        continue
                    yield RemoteMessage(
                        id=uid,
                        received_at=received,
                        received_origin="imap_internaldate",
                        raw_chunks=lambda session=session, uid=uid_bytes, size=raw_size: (
                            self._message_chunks(session, uid, uid_validity, size)
                        ),
                        raw_size=raw_size,
                        headers=headers,
                    )
                    if range_sync is not None:
                        range_sync.advance(uid)
                if sync is not None:
                    sync.next_cursor = str(next_uid)
                if range_sync is not None:
                    range_sync.finish()
            except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
                raise MailboxError(str(exc)) from exc
            finally:
                session.close()

        return scope, iterator()

    def fetch_message(
        self,
        target: MailTarget,
        remote_id: str,
        processing_namespace: str,
        password: str | None,
        *,
        access_token: str | None = None,
        refresh_access_token: Callable[[], str] | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> RemoteMessage | None:
        """Load one stable IMAP UID without enumerating current folder selection."""
        self._validate_authentication(target.account, password, access_token)
        uid = str(self._unsigned_number(remote_id, "stored message UID")).encode("ascii")
        session = _ImapReadSession(
            self, target, password, access_token, refresh_access_token, cancellation
        )
        try:
            uid_validity = session.read(
                lambda client: self._select_folder(client, target.folder, cancellation=cancellation)
            )
            session.uid_validity = uid_validity
            scope = imap_scope(target, uid_validity)
            if scope.processing_namespace != processing_namespace:
                raise MailboxError(
                    "IMAP UIDVALIDITY changed before the unfinished message could be downloaded."
                )
            existing = session.read(
                lambda client: self._recheck_uids(
                    client, {remote_id}, uid_validity, cancellation=cancellation
                )
            )
            if uid not in existing:
                session.close()
                return None
            metadata = session.read(
                lambda client: self._metadata_batch(
                    client, [uid], uid_validity, cancellation=cancellation
                )
            )
            item = metadata[uid]
            if isinstance(item, (RemoteMessageError, RemoteMessageUnavailable)):
                raise item
            received, raw_size, headers = item
            return RemoteMessage(
                id=remote_id,
                received_at=received,
                received_origin="imap_internaldate",
                raw_chunks=lambda: self._message_chunks(session, uid, uid_validity, raw_size),
                raw_size=raw_size,
                release=session.close,
                headers=headers,
            )
        except Exception as exc:
            session.close()
            if isinstance(
                exc, (OSError, ssl.SSLError, imaplib.IMAP4.error, UnicodeError, ValueError)
            ):
                raise MailboxError(str(exc)) from exc
            raise

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

    def _select_folder(
        self, client: imaplib.IMAP4, folder: str, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> str:
        for _ in range(2):
            cancellation.checkpoint()
            status, response = client.select(self._quoted_folder(folder), readonly=True)
            self._require_ok(status, response, f"Could not open mailbox folder '{folder}'.")
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
        received_between: tuple[datetime | None, datetime | None] | None = None,
        range_after: str | None = None,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[list[bytes], int]:
        cursor = (
            sync.cursor_for(scope.synchronization_namespace) if sync is not None else range_after
        )
        last_uid = (
            self._unsigned_number(cursor, "stored synchronization UID", allow_zero=True)
            if cursor is not None
            else 0
        )
        terms = [f"UID {min(last_uid + 1, 4294967295)}:*"] if cursor is not None else []
        if sync is None and received_between is not None:
            start, end = received_between
            # IMAP SEARCH compares calendar dates only. Enlarge the provider
            # preselection, then use exact INTERNALDATE in the service.
            if start:
                terms.append(f"SINCE {_imap_search_date(start - timedelta(days=1))}")
            if end:
                terms.append(f"BEFORE {_imap_search_date(end + timedelta(days=2))}")
        criterion = " ".join(terms) if terms else "ALL"
        cancellation.checkpoint()
        status, uid_data = client.uid("search", None, criterion)
        self._require_ok(status, uid_data, "Could not load the message list.")
        self._check_uidvalidity(client, uid_validity)
        # IMAP ranges are inclusive in either direction. n:* can return the last
        # message even when its UID is smaller than n.
        uids = sorted((uid for uid in self._search_uids(uid_data) if int(uid) > last_uid), key=int)
        next_uid = max((int(uid) for uid in uids), default=last_uid)
        if sync is not None:
            recheck_ids = sync.recheck_ids_for(scope.processing_namespace)
            if recheck_ids:
                existing = self._recheck_uids(
                    client, recheck_ids, uid_validity, cancellation=cancellation
                )
                sync.discarded_ids.update(recheck_ids - {uid.decode("ascii") for uid in existing})
                uids = sorted(set(uids) | existing, key=int)
            for uid in uids:
                sync.mark_present(uid.decode("ascii"))
        return uids, next_uid

    def _recheck_uids(
        self,
        client: imaplib.IMAP4,
        recheck_ids: set[str],
        uid_validity: str,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> set[bytes]:
        requested_ids = sorted(
            recheck_ids, key=lambda uid: self._unsigned_number(uid, "stored message UID")
        )
        existing: set[bytes] = set()
        for start in range(0, len(requested_ids), 500):
            cancellation.checkpoint()
            requested = ",".join(requested_ids[start : start + 500])
            status, uid_data = client.uid("search", None, f"UID {requested}")
            self._require_ok(status, uid_data, "Could not load messages requiring another check.")
            self._check_uidvalidity(client, uid_validity)
            existing.update(self._search_uids(uid_data))
        return {uid for uid in existing if uid.decode("ascii") in recheck_ids}

    def _parse_message_metadata_item(
        self, item: object, requested_uid: int, display_uid: str
    ) -> tuple[int, bytes, bytes] | None:
        if item is None or item == b"" or (isinstance(item, bytes) and item.strip() == b")"):
            return None
        if not isinstance(item, bytes):
            raise RemoteMessageError(f"Message {display_uid} returned ambiguous metadata.")
        uid_matches = re.findall(rb"\bUID ([0-9]+)\b", item, re.IGNORECASE)
        if len(uid_matches) != 1:
            raise RemoteMessageError(f"Message {display_uid} has no valid UID metadata.")
        response_uid = self._unsigned_number(uid_matches[0], "returned message UID")
        size_matches = re.findall(rb"\bRFC822\.SIZE ([0-9]+)\b", item, re.IGNORECASE)
        if len(size_matches) != 1:
            if response_uid == requested_uid and not size_matches:
                raise RemoteMessageError(f"Message {display_uid} has no valid size.")
            raise RemoteMessageError(f"Message {display_uid} returned ambiguous metadata.")
        date_matches = re.findall(rb'\bINTERNALDATE "([^"]+)"', item, re.IGNORECASE)
        if len(date_matches) != 1:
            if response_uid == requested_uid and not date_matches:
                raise RemoteMessageError(f"Message {display_uid} has no INTERNALDATE.")
            raise RemoteMessageError(f"Message {display_uid} returned ambiguous metadata.")
        return response_uid, size_matches[0], date_matches[0]

    def _metadata_values(
        self, record: tuple[int, bytes, bytes], display_uid: str
    ) -> tuple[datetime, int]:
        _, raw_size_value, received_value = record
        raw_size = self._message_size(raw_size_value)
        if raw_size == 0:
            raise RemoteMessageError(f"Message {display_uid} was empty.")
        try:
            received = _parse_internaldate(received_value)
        except ValueError as exc:
            raise RemoteMessageError("The IMAP INTERNALDATE is invalid.") from exc
        return received.astimezone(timezone.utc), raw_size

    @staticmethod
    def _metadata_records(response: list) -> list[tuple[bytes, list[tuple[bytes, bytes]]]]:
        """Join attributes around literals without treating header text as IMAP syntax."""
        records: list[tuple[bytes, list[tuple[bytes, bytes]]]] = []
        attributes = b""
        literals: list[tuple[bytes, bytes]] = []
        for item in response or []:
            if item is None or item == b"":
                continue
            prefix = item[0] if isinstance(item, tuple) and len(item) == 2 else item
            if not isinstance(prefix, bytes):
                raise RemoteMessageError("The IMAP server returned ambiguous metadata.")
            if re.match(rb"^[0-9]+ \(", prefix):
                if attributes:
                    records.append((attributes, literals))
                attributes, literals = b"", []
            elif not attributes:
                if prefix.strip() == b")":
                    continue
                raise RemoteMessageError("The IMAP server returned ambiguous metadata.")
            attributes += b" " + prefix
            if isinstance(item, tuple):
                if not isinstance(item[1], bytes):
                    raise RemoteMessageError("The IMAP server returned ambiguous metadata.")
                literals.append(item)
        if attributes:
            records.append((attributes, literals))
        return records

    @staticmethod
    def _batch_headers(literals: list[tuple[bytes, bytes]], raw_size: int) -> MailHeaders | None:
        if len(literals) != 1:
            return None
        prefix, raw = literals[0]
        marker = re.search(rb"BODY\[HEADER\]<0> \{([0-9]+)\}\s*$", prefix, re.IGNORECASE)
        if (
            marker is None
            or int(marker[1]) != len(raw)
            or len(raw) > IMAP_HEADER_BYTES
            or len(raw) > raw_size
            or not (raw.endswith(b"\r\n\r\n") or raw.endswith(b"\n\n"))
        ):
            # A partial field (or omitted section) must never disprove a rule.
            return None
        return parse_headers(raw)

    def _metadata_batch(
        self,
        client: imaplib.IMAP4,
        uids: list[bytes],
        uid_validity: str,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> dict[bytes, _MetadataResult]:
        if not 0 < len(uids) <= IMAP_METADATA_BATCH_SIZE:
            raise ValueError("Invalid IMAP metadata batch size.")
        requested = b",".join(uids)
        status, response = client.uid(
            "fetch",
            requested,
            f"(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER]<0.{IMAP_HEADER_BYTES}>)",
        )
        self._require_ok(status, response, f"Could not load message {uids[0].decode()} metadata.")
        self._check_uidvalidity(client, uid_validity)
        result: dict[bytes, _MetadataResult] = {
            uid: RemoteMessageUnavailable(f"IMAP message {uid.decode()} is no longer available.")
            for uid in uids
        }
        seen: set[bytes] = set()
        try:
            for attributes, literals in self._metadata_records(response):
                uid_values = re.findall(rb"\bUID ([0-9]+)\b", attributes, re.IGNORECASE)
                if len(uid_values) != 1 or uid_values[0] not in result:
                    raise RemoteMessageError("The IMAP server returned ambiguous metadata.")
                uid = uid_values[0]
                if uid in seen:
                    result[uid] = RemoteMessageError(
                        f"Message {uid.decode()} returned ambiguous metadata."
                    )
                    continue
                seen.add(uid)
                try:
                    record = self._parse_message_metadata_item(attributes, int(uid), uid.decode())
                    assert record is not None
                    received, size = self._metadata_values(record, uid.decode())
                    result[uid] = received, size, self._batch_headers(literals, size)
                except RemoteMessageError as exc:
                    result[uid] = exc
        except RemoteMessageError as exc:
            self._recheck_uids(
                client, {uid.decode() for uid in uids}, uid_validity, cancellation=cancellation
            )
            return dict.fromkeys(uids, exc)
        missing = {uid.decode() for uid in uids if uid not in seen}
        if missing:
            existing = self._recheck_uids(client, missing, uid_validity, cancellation=cancellation)
            for uid in existing:
                result[uid] = RemoteMessageError(
                    f"Message {uid.decode()} has no valid UID metadata."
                )
        return result

    def _message_chunks(
        self,
        session: _ImapReadSession,
        uid: bytes,
        uid_validity: str,
        raw_size: int,
    ) -> Iterator[bytes]:
        offset = 0
        while offset < raw_size:
            remaining = raw_size - offset
            count = min(MESSAGE_CHUNK_BYTES, remaining) + int(remaining <= MESSAGE_CHUNK_BYTES)
            chunk = session.read(
                lambda client, start=offset, length=count: self._message_chunk(
                    client, uid, uid_validity, start, length, cancellation=session.cancellation
                )
            )
            if not chunk:
                raise MailboxError(
                    f"Message {uid.decode(errors='replace')} ended before its declared size."
                )
            if len(chunk) > remaining:
                raise MailboxError(
                    f"Message {uid.decode(errors='replace')} exceeded its declared size."
                )
            offset += len(chunk)
            yield chunk
            session.cancellation.checkpoint()

    def _parse_message_chunk_item(
        self, item: object, requested_uid: int, offset: int
    ) -> tuple[bytes | None, bool]:
        if item is None or item == b"":
            return None, False
        if isinstance(item, bytes):
            return None, item.strip() != b")"
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            return None, True
        uid_matches = re.findall(rb"\bUID ([0-9]+)\b", item[0], re.IGNORECASE)
        offset_matches = re.findall(rb"\bBODY\[\]<([0-9]+)>", item[0], re.IGNORECASE)
        literal_matches = re.findall(rb"\{([0-9]+)\}", item[0])
        terminal_literal = re.search(rb"\{([0-9]+)\}\s*$", item[0])
        if (
            len(uid_matches) != 1
            or len(offset_matches) != 1
            or len(literal_matches) != 1
            or terminal_literal is None
        ):
            return None, True
        returned_uid = self._unsigned_number(uid_matches[0], "returned message UID")
        returned_offset = self._unsigned_number(
            offset_matches[0], "returned message offset", allow_zero=True
        )
        if returned_uid != requested_uid or returned_offset != offset:
            return None, True
        literal_size = self._unsigned_number(
            terminal_literal[1], "returned MIME literal size", allow_zero=True
        )
        if literal_size != len(item[1]):
            return None, True
        return item[1], False

    def _message_chunk(
        self,
        client: imaplib.IMAP4,
        uid: bytes,
        uid_validity: str,
        offset: int,
        count: int,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> bytes:
        status, response = client.uid("fetch", uid, f"(BODY.PEEK[]<{offset}.{count}>)")
        self._require_ok(
            status, response, f"Could not load message {uid.decode(errors='replace')}."
        )
        self._check_uidvalidity(client, uid_validity)
        requested_uid = self._unsigned_number(uid, "message UID")
        matching_chunks: list[bytes] = []
        invalid_chunk_response = False
        for item in response or []:
            chunk, invalid = self._parse_message_chunk_item(item, requested_uid, offset)
            invalid_chunk_response = invalid_chunk_response or invalid
            if chunk is not None:
                matching_chunks.append(chunk)
        raw = matching_chunks[0] if matching_chunks else None
        if raw is None or invalid_chunk_response or len(matching_chunks) > 1:
            existing = self._recheck_uids(
                client, {uid.decode("ascii")}, uid_validity, cancellation=cancellation
            )
            if uid not in existing:
                raise RemoteMessageUnavailable(
                    f"IMAP message {uid.decode(errors='replace')} is no longer available."
                )
            if len(matching_chunks) > 1:
                raise MailboxError(
                    f"Message {uid.decode(errors='replace')} returned an ambiguous MIME chunk."
                )
            if invalid_chunk_response:
                raise MailboxError(
                    f"Message {uid.decode(errors='replace')} returned an unexpected MIME chunk response."
                )
            raise MailboxError(
                f"Message {uid.decode(errors='replace')} did not return the requested MIME chunk."
            )
        if not raw:
            existing = self._recheck_uids(
                client, {uid.decode("ascii")}, uid_validity, cancellation=cancellation
            )
            if uid not in existing:
                raise RemoteMessageUnavailable(
                    f"IMAP message {uid.decode(errors='replace')} is no longer available."
                )
        if len(raw) > count:
            raise MailboxError(
                f"Message {uid.decode(errors='replace')} exceeded its requested chunk size."
            )
        return raw

    @staticmethod
    def _require_ok(status: str, response: list, message: str) -> None:
        if status == "OK":
            return
        if any(
            isinstance(line, bytes) and b"accesstokenexpired" in line.lower()
            for line in response or []
        ):
            raise _AccessTokenExpired("The IMAP OAuth access token expired (AccessTokenExpired).")
        raise MailboxError(message)

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

    @staticmethod
    def _message_size(value: bytes) -> int:
        """Parse RFC822.SIZE without applying the 32-bit UID limit."""
        try:
            decoded = value.decode("ascii")
            if re.fullmatch(r"0|[1-9][0-9]*", decoded) is None:
                raise ValueError
            return int(decoded)
        except (UnicodeError, ValueError) as exc:
            raise RemoteMessageError("The IMAP message size is invalid.") from exc

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
            if isinstance(exc, imaplib.IMAP4.error) and "accesstokenexpired" in str(exc).lower():
                raise _AccessTokenExpired(
                    "The IMAP OAuth access token expired (AccessTokenExpired)."
                ) from exc
            raise MailboxError(
                "IMAP OAuth authentication failed. Reauthorize the account and verify that IMAP "
                "access is enabled."
            ) from exc


class _ImapReadSession:
    """Renew an expired OAuth session without replaying already yielded messages."""

    def __init__(
        self,
        mailbox: ImapMailbox,
        target: MailTarget,
        password: str | None,
        access_token: str | None,
        refresh_access_token: Callable[[], str] | None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        self.mailbox = mailbox
        self.cancellation = cancellation
        self.target = target
        self.password = password
        self.access_token = access_token
        self.refresh_access_token = refresh_access_token
        self.client: imaplib.IMAP4 | None = None
        self.uid_validity: str | None = None

    def connect(self) -> None:
        self.cancellation.checkpoint()
        self.client = self.mailbox._connect(self.target.account, cancellation=self.cancellation)
        self.cancellation.checkpoint()
        if self.access_token is None:
            self.client.login(self.target.account.username, self.password)
        else:
            self.mailbox._authenticate_oauth(
                self.client, self.target.mailbox.address, self.access_token
            )

    def read(self, operation: Callable[[imaplib.IMAP4], _ReadResult]) -> _ReadResult:
        self.cancellation.checkpoint()
        try:
            if self.client is None:
                self.connect()
            assert self.client is not None
            self.cancellation.checkpoint()
            result = operation(self.client)
            self.cancellation.checkpoint()
            return result
        except (imaplib.IMAP4.error, _AccessTokenExpired) as exc:
            if (
                self.access_token is None
                or self.refresh_access_token is None
                or "accesstokenexpired" not in str(exc).lower()
            ):
                raise
        self.close()
        self.cancellation.checkpoint()
        self.access_token = self.refresh_access_token()
        self.connect()
        if self.uid_validity is not None:
            validity = self.mailbox._select_folder(
                self.client, self.target.folder, cancellation=self.cancellation
            )
            if validity != self.uid_validity:
                raise MailboxError("The IMAP UIDVALIDITY changed while reconnecting the folder.")
        # Retry once per read. A later expiry can recover again, but a broken
        # refresh or an immediately rejected replacement must not loop forever.
        self.cancellation.checkpoint()
        result = operation(self.client)
        self.cancellation.checkpoint()
        return result

    def close(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        if self.uid_validity is not None:
            try:
                client.close()
            except Exception:
                pass
        try:
            client.logout()
        except Exception:
            pass
