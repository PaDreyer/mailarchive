from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation
from mailarchive.application.source_port import (
    MailboxError,
    MessageFilter,
    RemoteMessage,
    RemoteMessageError,
    RemoteMessageOutsideScope,
    RemoteMessageUnavailable,
    ScanWideProviderError,
)
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import Account, Mailbox, MailHeaders
from mailarchive.domain.mail_parser import parse_header_pairs
from mailarchive.domain.source_identity import MailTarget, MessageScope, api_scope
from mailarchive.infrastructure.oauth import OAuthManager
from mailarchive.infrastructure.providers.http import (
    HttpClient,
    ProviderHttpError,
    _message_http_error,
    _nonempty_string,
    _OAuthHttpSession,
    _object_list,
    _optional_string,
    _scan_wide_http_error,
)


def _label_ids(payload: dict) -> list[str]:
    value = payload.get("labelIds", [])
    if not isinstance(value, list) or any(
        not isinstance(label, str) or not label for label in value
    ):
        raise RemoteMessageError("Gmail returned invalid label IDs.")
    return value


def _history_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not value.strip("0")
    ):
        raise MailboxError("Gmail did not return a synchronization history ID.")
    return value


class GmailMessageSource:
    API_ROOT = "https://gmail.googleapis.com/gmail/v1/users"

    def __init__(self, oauth: OAuthManager, http: HttpClient | None = None) -> None:
        self.oauth = oauth
        self.http = http or HttpClient()

    def targets(
        self, account: Account, mailbox: Mailbox, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[MailTarget]:
        return [MailTarget(account, mailbox, "")]

    def fetch_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        *,
        sync: SyncSession | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        cancellation.checkpoint()
        access_token = self.oauth.google_access_token(
            target.account, mailbox_address=target.mailbox.address
        )
        scan = _GmailMailboxScan(
            self, target, access_token, should_fetch, sync, cancellation=cancellation
        )
        return scan.scope, scan.messages()

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
        token = self.oauth.google_access_token(
            target.account, mailbox_address=target.mailbox.address
        )
        scan = _GmailMailboxScan(
            self,
            target,
            token,
            should_fetch,
            None,
            received_between=(start, end),
            range_sync=range_sync,
            cancellation=cancellation,
        )
        return scan.scope, scan.messages()

    def fetch_message(
        self,
        target: MailTarget,
        remote_id: str,
        processing_namespace: str,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> RemoteMessage | None:
        cancellation.checkpoint()
        scan = _GmailMailboxScan(
            self,
            target,
            self.oauth.google_access_token(target.account, mailbox_address=target.mailbox.address),
            lambda _scope, _remote_id: True,
            None,
            cancellation=cancellation,
        )
        if scan.scope.processing_namespace != processing_namespace:
            raise MailboxError("The unfinished Gmail message belongs to a different mailbox.")
        try:
            remote = next(scan._fetch(remote_id), None)
        except ProviderHttpError as exc:
            if exc.status == 404:
                return None
            raise
        if remote is not None and isinstance(remote.error, RemoteMessageUnavailable):
            return None
        if remote is not None and isinstance(remote.error, RemoteMessageOutsideScope):
            raise remote.error
        return remote


class _GmailMailboxScan:
    """Own one mailbox enumeration, including pagination, downloads, and rechecks."""

    def __init__(
        self,
        source: GmailMessageSource,
        target: MailTarget,
        access_token: str,
        should_fetch: MessageFilter,
        sync: SyncSession | None,
        received_between: tuple[datetime | None, datetime | None] | None = None,
        range_sync: RangePagination | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        self.cancellation = cancellation
        self.http = _OAuthHttpSession(
            source.http,
            access_token,
            lambda: source.oauth.google_access_token(
                target.account, mailbox_address=target.mailbox.address, force_refresh=True
            ),
            cancellation=cancellation,
        )
        self.api_root = f"{source.API_ROOT}/{quote(target.mailbox.address.strip(), safe='')}"
        self.should_fetch = should_fetch
        self.sync = sync
        self.received_between = received_between
        self.range_sync = range_sync
        self.labels = set(target.selected_folders or target.mailbox.folders)
        self.scope = api_scope(target)
        self.seen: set[str] = set()

    def _selected(self, message_labels: list[str]) -> bool:
        return not self.labels or bool(self.labels.intersection(message_labels))

    def _full_ids(self) -> Iterator[str]:
        if self.sync is not None:
            profile = self.http.get_json(f"{self.api_root}/profile?fields=historyId")
            cursor = _history_id(profile.get("historyId"))
        for page in self._full_pages():
            for item in _object_list(page, "messages"):
                message_id = _nonempty_string(item.get("id"), "Gmail message ID")
                if self.sync is not None:
                    self.sync.mark_present(message_id)
                yield message_id
        if self.sync is not None:
            # Capture before listing: changes during the full scan are replayed next time.
            self.sync.next_cursor = cursor

    def _range_position(self, labels: list[str]) -> tuple[int, str | None]:
        if self.range_sync is None:
            return 0, None
        saved = self.range_sync.start(self.scope.processing_namespace)
        if saved is None:
            return 0, None
        try:
            position = json.loads(saved)
            label = position["label"]
            page = position["page"]
            if not isinstance(label, str) or (page is not None and not isinstance(page, str)):
                raise TypeError
            return labels.index(label), page
        except (KeyError, TypeError, ValueError) as exc:
            raise MailboxError("The saved Gmail range checkpoint is invalid.") from exc

    def _full_page_parameters(self, label: str, page_token: str | None) -> dict[str, str]:
        parameters = {"labelIds": label} if label else {}
        parameters.update({"maxResults": "500", "includeSpamTrash": "true"})
        if self.received_between is not None:
            start, end = self.received_between
            terms = []
            if start:
                terms.append(f"after:{int(start.timestamp()) - 1}")
            if end:
                terms.append(f"before:{int(end.timestamp()) + 1}")
            if terms:
                parameters["q"] = " ".join(terms)
        if page_token:
            parameters["pageToken"] = page_token
        return parameters

    def _save_gmail_position(self, label: str, page_token: str | None) -> None:
        if self.range_sync is not None:
            self.range_sync.advance(
                json.dumps(
                    {"label": label, "page": page_token},
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )

    def _full_pages(self) -> Iterator[dict[str, Any]]:
        labels = sorted(self.labels) or [""]
        label_index, page_token = self._range_position(labels)
        reset_attempted = False
        while label_index < len(labels):
            label = labels[label_index]
            visited: set[str] = set()
            while True:
                parameters = self._full_page_parameters(label, page_token)
                try:
                    page = self.http.get_json(
                        f"{self.api_root}/messages?{urlencode(parameters)}",
                    )
                except ProviderHttpError as exc:
                    if (
                        self.range_sync is None
                        or page_token is None
                        or reset_attempted
                        or exc.status not in {400, 404, 410}
                    ):
                        raise
                    self.range_sync.reset()
                    self.seen.clear()
                    label_index = 0
                    page_token = None
                    reset_attempted = True
                    break
                yield page
                page_token = _optional_string(page, "nextPageToken")
                if page_token is None:
                    label_index += 1
                    if label_index < len(labels):
                        self._save_gmail_position(labels[label_index], None)
                    elif self.range_sync is not None:
                        self.range_sync.finish()
                    break
                if page_token in visited:
                    raise MailboxError("Gmail returned a repeating message page token.")
                visited.add(page_token)
                self._save_gmail_position(label, page_token)

    def _history_ids(self, cursor: str) -> Iterator[str]:
        _history_id(cursor)
        page_token: str | None = None
        visited: set[str] = set()
        while True:
            parameters = {"startHistoryId": cursor, "maxResults": "500"}
            if page_token:
                parameters["pageToken"] = page_token
            try:
                page = self.http.get_json(f"{self.api_root}/history?{urlencode(parameters)}")
            except ProviderHttpError as exc:
                if exc.status != 404:
                    raise
                assert self.sync is not None
                self.sync.report_reset()
                self.seen.clear()
                yield from self._full_ids()
                return
            next_cursor = _history_id(page.get("historyId"))
            normalized_next = next_cursor.lstrip("0")
            normalized_start = cursor.lstrip("0")
            if (len(normalized_next), normalized_next) < (
                len(normalized_start),
                normalized_start,
            ):
                raise MailboxError("Gmail returned a history ID older than the stored cursor.")
            yield from self._changed_message_ids(page)
            page_token = _optional_string(page, "nextPageToken")
            if page_token is None:
                assert self.sync is not None
                self.sync.next_cursor = next_cursor
                return
            if page_token in visited:
                raise MailboxError("Gmail returned a repeating history page token.")
            visited.add(page_token)

    def _changed_message_ids(self, page: dict[str, Any]) -> Iterator[str]:
        for history in _object_list(page, "history"):
            for added in _object_list(history, "messagesAdded"):
                message = added.get("message")
                if not isinstance(message, dict):
                    raise MailboxError("Gmail returned an invalid added message.")
                message_id = _nonempty_string(message.get("id"), "Gmail message ID")
                try:
                    selected = "labelIds" not in message or self._selected(_label_ids(message))
                except RemoteMessageError:
                    selected = True
                if selected:
                    yield message_id
            for added in _object_list(history, "labelsAdded"):
                message = added.get("message")
                if not isinstance(message, dict):
                    raise MailboxError("Gmail returned an invalid label change message.")
                message_id = _nonempty_string(message.get("id"), "Gmail message ID")
                try:
                    selected = self._selected(_label_ids(added))
                except RemoteMessageError:
                    selected = True
                if selected:
                    yield message_id

    def _fetch(self, message_id: str) -> Iterator[RemoteMessage]:
        self.cancellation.checkpoint()
        message_url = f"{self.api_root}/messages/{quote(message_id, safe='')}"
        if self.sync is not None and self.sync.baseline:
            self.should_fetch(self.scope, message_id)
            return
        reserved = False
        try:
            metadata = self.http.get_json(
                self._metadata_url(message_url)
                if self.http.supports_gmail_streaming
                else f"{message_url}?format=minimal&fields=internalDate,labelIds"
            )
            if not self._selected(_label_ids(metadata)):
                if self.sync is not None:
                    self.sync.discard(message_id)
                yield RemoteMessage(
                    id=message_id,
                    error=RemoteMessageOutsideScope(
                        f"Gmail message {message_id} left every selected source label."
                    ),
                )
                return
            if self.sync is not None:
                self.sync.mark_present(message_id)
            if not self.should_fetch(self.scope, message_id):
                return
            reserved = True
            raw, raw_chunks, message = self._message_body(message_url, message_id, metadata)
            received = self._received(message, message_id)
        except ProviderHttpError as exc:
            if _scan_wide_http_error(exc):
                raise
            if exc.status == 404 and self.sync is not None:
                self.sync.discard(message_id)
                return
            if not reserved and not self.should_fetch(self.scope, message_id):
                return
            yield RemoteMessage(id=message_id, error=_message_http_error("Gmail", message_id, exc))
            return
        except RemoteMessageError as exc:
            if not reserved and not self.should_fetch(self.scope, message_id):
                return
            yield RemoteMessage(id=message_id, error=exc)
            return
        yield RemoteMessage(
            id=message_id,
            raw=raw,
            received_at=received,
            received_origin="gmail_internal_date",
            raw_chunks=raw_chunks,
            headers=self._headers(message),
        )

    def _message_body(
        self, message_url: str, message_id: str, metadata: dict[str, Any]
    ) -> tuple[bytes | None, Callable[[], Iterator[bytes]] | None, dict[str, Any]]:
        if self.http.supports_gmail_streaming:
            return (
                None,
                self._raw_chunks(
                    message_id,
                    self.http.gmail_raw_chunks(f"{message_url}?format=raw&fields=raw"),
                ),
                metadata,
            )
        message = self.http.get_json(f"{message_url}?format=raw&fields=raw,internalDate,labelIds")
        encoded = message.get("raw")
        if not isinstance(encoded, str) or not encoded:
            raise RemoteMessageError(f"Gmail message {message_id} did not contain MIME data.")
        try:
            raw = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        except ValueError as exc:
            raise RemoteMessageError(
                f"Gmail message {message_id} contained invalid MIME data."
            ) from exc
        return raw, None, message

    @staticmethod
    def _received(message: dict[str, Any], message_id: str) -> datetime:
        value = message.get("internalDate")
        if not isinstance(value, str) or not value.isdecimal():
            raise RemoteMessageError(f"Gmail message {message_id} has no valid internalDate.")
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise RemoteMessageError(
                f"Gmail message {message_id} has an invalid internalDate."
            ) from exc

    @staticmethod
    def _metadata_url(message_url: str) -> str:
        parameters = [
            ("format", "metadata"),
            ("fields", "internalDate,labelIds,payload/headers"),
            *(("metadataHeaders", name) for name in ("From", "To", "Cc", "Bcc", "Subject", "Date")),
        ]
        return f"{message_url}?{urlencode(parameters)}"

    @staticmethod
    def _headers(message: dict[str, Any]) -> MailHeaders | None:
        payload = message.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("headers"), list):
            return None
        pairs = []
        for header in payload["headers"]:
            if (
                not isinstance(header, dict)
                or not isinstance(header.get("name"), str)
                or not isinstance(header.get("value"), str)
            ):
                return None
            pairs.append((header["name"], header["value"]))
        return parse_header_pairs(pairs)

    def _raw_chunks(
        self, message_id: str, chunks: Callable[[], Iterator[bytes]]
    ) -> Callable[[], Iterator[bytes]]:
        def read() -> Iterator[bytes]:
            try:
                yield from chunks()
            except ProviderHttpError as exc:
                if _scan_wide_http_error(exc):
                    raise ScanWideProviderError(str(exc)) from exc
                raise _message_http_error("Gmail", message_id, exc) from exc

        return read

    def messages(self) -> Iterator[RemoteMessage]:
        cursor = (
            self.sync.cursor_for(self.scope.synchronization_namespace)
            if self.sync is not None
            else None
        )
        ids = self._history_ids(cursor) if cursor is not None else self._full_ids()
        for message_id in ids:
            if message_id and message_id not in self.seen:
                self.seen.add(message_id)
                yield from self._fetch(message_id)
                if self.sync is not None and message_id in self.sync.discarded_ids:
                    self.seen.discard(message_id)
        if self.sync is not None:
            for message_id in sorted(
                self.sync.recheck_ids_for(self.scope.processing_namespace) - self.seen
            ):
                yield from self._fetch(message_id)
