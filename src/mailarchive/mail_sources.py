from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mailarchive.credential_data import load_credential_data
from mailarchive.credentials import CredentialStore
from mailarchive.imap_client import (
    ImapMailbox,
    MailboxError,
    RemoteMessage,
    RemoteMessageError,
    RemoteMessageUnavailable,
)
from mailarchive.intake_limits import (
    MAX_GMAIL_WIRE_BYTES,
    MAX_JSON_BYTES,
    MAX_MESSAGE_BYTES,
    MESSAGE_CHUNK_BYTES,
    MessageTooLargeError,
)
from mailarchive.mail_identity import MailTarget, MessageScope, api_scope
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from mailarchive.oauth import OAuthManager
from mailarchive.synchronization import RangePagination, SyncSession

MessageFilter = Callable[[MessageScope, str], bool]
_HttpResult = TypeVar("_HttpResult")
_GMAIL_RAW_MARKER = re.compile(rb'"raw"\s*:\s*"')
_BASE64URL = re.compile(rb"[A-Za-z0-9_\-=]*\Z")


def _https_origin(url: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return parsed.hostname.casefold(), port or 443


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow provider redirects only within the original HTTPS origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_origin = _https_origin(newurl)
        if target_origin is None or _https_origin(req.full_url) != target_origin:
            raise HTTPError(
                newurl,
                502,
                "The mail provider redirected the request to an untrusted origin.",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request: Request, *, timeout: int):
    """Open one provider request with an origin-bound redirect policy."""
    return build_opener(_SameOriginRedirectHandler()).open(request, timeout=timeout)


def _decode_gmail_raw(wire: Iterator[bytes]) -> Iterator[bytes]:
    """Incrementally extract and decode the requested Gmail raw JSON field."""
    prefix = b""
    carry = b""
    found = False
    complete = False
    try:
        for chunk in wire:
            if complete:
                continue
            data = chunk
            if not found:
                prefix += data
                marker = _GMAIL_RAW_MARKER.search(prefix)
                if marker is None:
                    if len(prefix) > 64 * 1024:
                        raise MailboxError(
                            "Gmail did not return MIME data near the response start."
                        )
                    continue
                data = prefix[marker.end() :]
                prefix = b""
                found = True
            closing = data.find(b'"')
            encoded = data if closing < 0 else data[:closing]
            if _BASE64URL.fullmatch(encoded) is None:
                raise MailboxError("Gmail returned invalid encoded MIME data.")
            combined = carry + encoded
            if closing < 0:
                usable = len(combined) // 4 * 4
                # Padding terminates base64 and must be decoded with the final group.
                padding = combined.find(b"=")
                if 0 <= padding < usable:
                    usable = padding // 4 * 4
                if usable:
                    try:
                        yield base64.b64decode(combined[:usable], altchars=b"-_", validate=True)
                    except binascii.Error as exc:
                        raise MailboxError("Gmail returned invalid encoded MIME data.") from exc
                carry = combined[usable:]
                continue
            try:
                decoded = base64.b64decode(
                    combined + b"=" * (-len(combined) % 4),
                    altchars=b"-_",
                    validate=True,
                )
            except binascii.Error as exc:
                raise MailboxError("Gmail returned invalid encoded MIME data.") from exc
            if decoded:
                yield decoded
            complete = True
        if complete:
            return
        raise MailboxError("Gmail did not return complete MIME data.")
    finally:
        close = getattr(wire, "close", None)
        if close is not None:
            close()


def _object_list(payload: dict, key: str, *, required: bool = False) -> list[dict]:
    value = payload.get(key, [] if not required else None)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MailboxError(f"The mail provider returned an invalid {key} list.")
    return value


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MailboxError(f"The mail provider did not return a valid {name}.")
    return value


def _optional_string(payload: dict, key: str) -> str | None:
    return _nonempty_string(payload[key], key) if key in payload else None


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


class MessageSource(Protocol):
    def targets(self, account: Account, mailbox: Mailbox) -> list[MailTarget]: ...

    def fetch_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        *,
        sync: SyncSession | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]: ...

    def fetch_message(
        self, target: MailTarget, remote_id: str, processing_namespace: str
    ) -> RemoteMessage | None: ...


class ProviderHttpError(MailboxError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"The mail provider returned HTTP {status}: {detail[:500]}")
        self.status = status
        self.code = ""
        try:
            value = json.loads(detail)
            error = value.get("error", {}) if isinstance(value, dict) else {}
            if isinstance(error, dict):
                self.code = str(error.get("code", ""))
        except ValueError:
            pass

    @property
    def scan_wide(self) -> bool:
        return self.status in {401, 403, 429}


class ScanWideProviderError(MailboxError):
    """A lazy message stream hit a provider failure that must stop discovery."""


def _scan_wide_http_error(error: ProviderHttpError) -> bool:
    """Keep authentication, authorization, and throttling failures scan-wide."""
    return error.scan_wide


def _message_http_error(
    provider: str, message_id: str, error: ProviderHttpError
) -> RemoteMessageError:
    if error.status == 404:
        return RemoteMessageUnavailable(f"{provider} message {message_id} is no longer available.")
    return RemoteMessageError(f"Could not load {provider} message {message_id}: {error}")


class HttpClient:
    def _declared_response_length(
        self, response_headers: Any, max_bytes: int, capacity_error: bool
    ) -> int | None:
        get_all = getattr(response_headers, "get_all", None)
        if callable(get_all):
            length_values = get_all("Content-Length") or []
            if len(length_values) > 1:
                raise MailboxError("The mail provider returned ambiguous response sizes.")
            length_value = length_values[0] if length_values else None
        else:
            length_value = response_headers.get("Content-Length")
        if length_value is None:
            return None
        if not isinstance(length_value, str) or re.fullmatch(r"[0-9]+", length_value) is None:
            raise MailboxError("The mail provider returned an invalid response size.")
        declared_length = int(length_value)
        if declared_length > max_bytes:
            self._raise_size_limit(capacity_error)
        return declared_length

    def get_json(
        self,
        url: str,
        access_token: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raw = self.get_bytes(url, access_token, headers)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise MailboxError("The mail provider returned an invalid JSON response.") from exc
        if not isinstance(value, dict):
            raise MailboxError("The mail provider returned an unexpected response.")
        return value

    def get_bytes(
        self,
        url: str,
        access_token: str,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return b"".join(self.iter_bytes(url, access_token, headers, max_bytes=MAX_JSON_BYTES))

    def iter_bytes(
        self,
        url: str,
        access_token: str,
        headers: dict[str, str] | None = None,
        *,
        max_bytes: int = MAX_MESSAGE_BYTES,
        capacity_error: bool = False,
    ) -> Iterator[bytes]:
        if _https_origin(url) is None:
            raise MailboxError("The mail provider returned an invalid secure URL.")
        request_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "MailArchive/0.0.1",
        }
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers)
        try:
            with urlopen(request, timeout=30) as response:
                response_headers = getattr(response, "headers", {})
                declared_length = self._declared_response_length(
                    response_headers, max_bytes, capacity_error
                )
                total = 0
                while True:
                    chunk = response.read(MESSAGE_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        self._raise_size_limit(capacity_error)
                    if declared_length is not None and total > declared_length:
                        raise MailboxError("The mail provider response exceeded its declared size.")
                    yield chunk
                if declared_length is not None and total < declared_length:
                    raise MailboxError("The mail provider response ended before its declared size.")
        except HTTPError as exc:
            try:
                detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise ProviderHttpError(exc.code, detail) from exc
        except (OSError, URLError) as exc:
            raise MailboxError(str(exc)) from exc

    @staticmethod
    def _raise_size_limit(capacity_error: bool) -> None:
        message = "The mail provider response exceeds its size limit."
        if capacity_error:
            raise MessageTooLargeError(message)
        raise MailboxError(message)

    def iter_gmail_raw(
        self,
        url: str,
        access_token: str,
        headers: dict[str, str] | None = None,
    ) -> Iterator[bytes]:
        wire = self.iter_bytes(
            url,
            access_token,
            headers,
            max_bytes=MAX_GMAIL_WIRE_BYTES,
            capacity_error=True,
        )
        yield from _decode_gmail_raw(wire)


class _OAuthHttpSession:
    """Keep renewed tokens local to one mailbox scan or folder discovery."""

    def __init__(
        self, http: HttpClient, access_token: str, refresh_access_token: Callable[[], str]
    ) -> None:
        self.http = http
        self.access_token = access_token
        self.refresh_access_token = refresh_access_token

    def _read(self, request: Callable[[str], _HttpResult]) -> _HttpResult:
        try:
            return request(self.access_token)
        except ProviderHttpError as exc:
            if exc.status != 401:
                raise
        self.access_token = self.refresh_access_token()
        # Repeat this request once, preserving URL, pagination state and headers.
        # A later expiry can renew again; a rejected replacement ends the request.
        return request(self.access_token)

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return self._read(lambda token: self.http.get_json(url, token, headers))

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        return self._read(lambda token: self.http.get_bytes(url, token, headers))

    @property
    def supports_streaming(self) -> bool:
        return callable(getattr(self.http, "iter_bytes", None))

    @property
    def supports_gmail_streaming(self) -> bool:
        return callable(getattr(self.http, "iter_gmail_raw", None))

    def message_chunks(
        self, url: str, headers: dict[str, str] | None = None
    ) -> Callable[[], Iterator[bytes]]:
        return lambda: self._stream(
            lambda token: self.http.iter_bytes(url, token, headers, capacity_error=True)
        )

    def gmail_raw_chunks(
        self, url: str, headers: dict[str, str] | None = None
    ) -> Callable[[], Iterator[bytes]]:
        return lambda: self._stream(lambda token: self.http.iter_gmail_raw(url, token, headers))

    def _stream(self, request: Callable[[str], Iterator[bytes]]) -> Iterator[bytes]:
        yielded = False
        try:
            for chunk in request(self.access_token):
                yielded = True
                yield chunk
            return
        except ProviderHttpError as exc:
            if exc.status != 401 or yielded:
                raise
        self.access_token = self.refresh_access_token()
        yield from request(self.access_token)


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


class GmailMessageSource:
    API_ROOT = "https://gmail.googleapis.com/gmail/v1/users"

    def __init__(self, oauth: OAuthManager, http: HttpClient | None = None) -> None:
        self.oauth = oauth
        self.http = http or HttpClient()

    def targets(self, account: Account, mailbox: Mailbox) -> list[MailTarget]:
        return [MailTarget(account, mailbox, "")]

    def fetch_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        *,
        sync: SyncSession | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        access_token = self.oauth.google_access_token(
            target.account, mailbox_address=target.mailbox.address
        )
        scan = _GmailMailboxScan(self, target, access_token, should_fetch, sync)
        return scan.scope, scan.messages()

    def search_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        start: datetime | None,
        end: datetime | None,
        *,
        range_sync: RangePagination | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
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
        )
        return scan.scope, scan.messages()

    def fetch_message(
        self, target: MailTarget, remote_id: str, processing_namespace: str
    ) -> RemoteMessage | None:
        scan = _GmailMailboxScan(
            self,
            target,
            self.oauth.google_access_token(target.account, mailbox_address=target.mailbox.address),
            lambda _scope, _remote_id: True,
            None,
        )
        if scan.scope.processing_namespace != processing_namespace:
            raise MailboxError("The unfinished Gmail message belongs to a different mailbox.")
        try:
            remote = next(scan._fetch(remote_id, verify_label=False), None)
        except ProviderHttpError as exc:
            if exc.status == 404:
                return None
            raise
        if remote is not None and isinstance(remote.error, RemoteMessageUnavailable):
            return None
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
    ) -> None:
        self.http = _OAuthHttpSession(
            source.http,
            access_token,
            lambda: source.oauth.google_access_token(
                target.account, mailbox_address=target.mailbox.address, force_refresh=True
            ),
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

    def _fetch(self, message_id: str, verify_label: bool) -> Iterator[RemoteMessage]:
        message_url = f"{self.api_root}/messages/{quote(message_id, safe='')}"
        metadata = None
        if not self.should_fetch(self.scope, message_id):
            return
        try:
            if verify_label:
                metadata = self.http.get_json(
                    f"{message_url}?format=minimal&fields=internalDate,labelIds"
                )
                if not self._selected(_label_ids(metadata)):
                    assert self.sync is not None
                    self.sync.discarded_ids.add(message_id)
                    return
                assert self.sync is not None
                self.sync.mark_present(message_id)
            if self.http.supports_gmail_streaming:
                if metadata is None:
                    fields = "internalDate,labelIds" if self.sync is not None else "internalDate"
                    metadata = self.http.get_json(f"{message_url}?format=minimal&fields={fields}")
                raw = None
                raw_chunks = self._raw_chunks(
                    message_id,
                    self.http.gmail_raw_chunks(f"{message_url}?format=raw&fields=raw"),
                )
                message = metadata
            else:
                fields = (
                    "raw,internalDate,labelIds" if self.sync is not None else "raw,internalDate"
                )
                message = self.http.get_json(f"{message_url}?format=raw&fields={fields}")
                encoded = message.get("raw")
                if not isinstance(encoded, str) or not encoded:
                    raise RemoteMessageError(
                        f"Gmail message {message_id} did not contain MIME data."
                    )
                padding = "=" * (-len(encoded) % 4)
                try:
                    raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
                except ValueError as exc:
                    raise RemoteMessageError(
                        f"Gmail message {message_id} contained invalid MIME data."
                    ) from exc
                raw_chunks = None
            if self.sync is not None and not self._selected(_label_ids(message)):
                self.sync.discarded_ids.add(message_id)
                return
            value = message.get("internalDate")
            if not isinstance(value, str) or not value.isdecimal():
                raise RemoteMessageError(f"Gmail message {message_id} has no valid internalDate.")
            try:
                received = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
            except (OSError, OverflowError, ValueError) as exc:
                raise RemoteMessageError(
                    f"Gmail message {message_id} has an invalid internalDate."
                ) from exc
        except ProviderHttpError as exc:
            if _scan_wide_http_error(exc):
                raise
            if exc.status == 404 and self.sync is not None:
                self.sync.discarded_ids.add(message_id)
                return
            yield RemoteMessage(id=message_id, error=_message_http_error("Gmail", message_id, exc))
            return
        except RemoteMessageError as exc:
            yield RemoteMessage(id=message_id, error=exc)
            return
        yield RemoteMessage(
            id=message_id,
            raw=raw,
            received_at=received,
            received_origin="gmail_internal_date",
            raw_chunks=raw_chunks,
        )

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
                yield from self._fetch(message_id, verify_label=cursor is not None)
                if self.sync is not None and message_id in self.sync.discarded_ids:
                    self.seen.discard(message_id)
        if self.sync is not None:
            for message_id in sorted(
                self.sync.recheck_ids_for(self.scope.processing_namespace) - self.seen
            ):
                yield from self._fetch(message_id, verify_label=True)


class MicrosoftGraphMessageSource:
    API_ROOT = "https://graph.microsoft.com/v1.0"
    GRAPH_HEADERS = {"Prefer": 'IdType="ImmutableId"'}

    def __init__(self, oauth: OAuthManager, http: HttpClient | None = None) -> None:
        self.oauth = oauth
        self.http = http or HttpClient()

    def targets(self, account: Account, mailbox: Mailbox) -> list[MailTarget]:
        folders = mailbox.folders or self.list_folders(MailTarget(account, mailbox, ""))
        return [MailTarget(account, mailbox, folder, tuple(folders)) for folder in folders]

    def list_folders(self, target: MailTarget) -> list[str]:
        account = target.account
        http = _OAuthHttpSession(
            self.http,
            self.oauth.microsoft_access_token(account),
            lambda: self.oauth.microsoft_access_token(account, force_refresh=True),
        )
        root = self._mailbox_root(target)
        parameters = urlencode({"$select": "id,childFolderCount", "includeHiddenFolders": "true"})
        pending = [f"{self.API_ROOT}{root}/mailFolders?{parameters}"]
        folders: list[str] = []
        seen: set[str] = set()
        visited: set[str] = set()
        while pending:
            url = pending.pop(0)
            parsed = urlsplit(url)
            if (
                (parsed.scheme, parsed.netloc) != ("https", "graph.microsoft.com")
                or not parsed.path.startswith("/v1.0/")
                or parsed.fragment
            ):
                raise MailboxError("Microsoft returned an invalid folder continuation link.")
            if url in visited:
                raise MailboxError("Microsoft returned a repeating folder continuation link.")
            visited.add(url)
            page = http.get_json(url, self.GRAPH_HEADERS)
            if not isinstance(page.get("value"), list):
                raise MailboxError("Microsoft returned an unexpected folder list.")
            for item in _object_list(page, "value", required=True):
                folder = item.get("id")
                if not isinstance(folder, str) or not folder:
                    raise MailboxError("Microsoft did not return a folder ID.")
                if folder in seen:
                    continue
                seen.add(folder)
                # Search folders are virtual views; physical folders cover their messages.
                if item.get("@odata.type") == "#microsoft.graph.mailSearchFolder":
                    continue
                folders.append(folder)
                child_count = item.get("childFolderCount")
                if type(child_count) is not int or child_count < 0:
                    raise MailboxError("Microsoft did not return a valid child folder count.")
                if child_count:
                    pending.append(
                        f"{self.API_ROOT}{root}/mailFolders/{quote(folder, safe='')}/childFolders?{parameters}"
                    )
            next_page = _optional_string(page, "@odata.nextLink")
            if next_page is not None:
                pending.append(next_page)
        return folders

    @staticmethod
    def _mailbox_root(target: MailTarget) -> str:
        account = target.account
        if (
            account.auth_mode == AuthMode.OAUTH_APPLICATION
            or target.mailbox.address.strip().casefold() != account.username.strip().casefold()
        ):
            return f"/users/{quote(target.mailbox.address, safe='')}"
        return "/me"

    def fetch_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        *,
        sync: SyncSession | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        access_token = self.oauth.microsoft_access_token(target.account)
        scan = _GraphFolderScan(self, target, access_token, should_fetch, sync)
        return scan.scope, scan.messages()

    def search_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        start: datetime | None,
        end: datetime | None,
        *,
        range_sync: RangePagination | None = None,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        token = self.oauth.microsoft_access_token(target.account)
        scan = _GraphFolderScan(
            self,
            target,
            token,
            should_fetch,
            None,
            received_between=(start, end),
            range_sync=range_sync,
        )
        return scan.scope, scan.messages()

    def fetch_message(
        self, target: MailTarget, remote_id: str, processing_namespace: str
    ) -> RemoteMessage | None:
        scan = _GraphFolderScan(
            self,
            target,
            self.oauth.microsoft_access_token(target.account),
            lambda _scope, _remote_id: True,
            None,
        )
        if scan.scope.processing_namespace != processing_namespace:
            raise MailboxError("The unfinished Microsoft message belongs to a different mailbox.")
        try:
            remote = next(scan._fetch(remote_id), None)
        except ProviderHttpError as exc:
            if exc.status == 404:
                return None
            raise
        if remote is not None and isinstance(remote.error, RemoteMessageUnavailable):
            return None
        return remote


class _GraphFolderScan:
    """Own one folder delta scan and its mailbox-wide targeted rechecks."""

    def __init__(
        self,
        source: MicrosoftGraphMessageSource,
        target: MailTarget,
        access_token: str,
        should_fetch: MessageFilter,
        sync: SyncSession | None,
        received_between: tuple[datetime | None, datetime | None] | None = None,
        range_sync: RangePagination | None = None,
    ) -> None:
        self.http = _OAuthHttpSession(
            source.http,
            access_token,
            lambda: source.oauth.microsoft_access_token(target.account, force_refresh=True),
        )
        self.api_root = source.API_ROOT
        self.headers = source.GRAPH_HEADERS
        self.target = target
        self.should_fetch = should_fetch
        self.sync = sync
        self.received_between = received_between
        self.range_sync = range_sync
        self.folder = target.folder or "inbox"
        self.folder_path = quote(self.folder, safe="")
        self.mailbox_root = source._mailbox_root(target)
        self.scope = api_scope(target)
        params = {"$select": "id", "$top": "999"}
        if received_between is not None:
            start, end = received_between
            filters = []
            if start:
                filters.append(f"receivedDateTime ge {start.isoformat().replace('+00:00', 'Z')}")
            if end:
                filters.append(f"receivedDateTime lt {end.isoformat().replace('+00:00', 'Z')}")
            if filters:
                params["$filter"] = " and ".join(filters)
        parameters = urlencode(params)
        self.first_page = (
            f"{self.api_root}{self.mailbox_root}/mailFolders/{self.folder_path}/messages"
            f"{'/delta' if sync is not None else ''}?{parameters}"
        )
        self.resolved_folder_id: str | None = None
        self.resolved_folders: dict[str, str] = {}
        self.seen: set[str] = set()

    def _trusted_link(self, value: str) -> str:
        parsed = urlsplit(value)
        root = urlsplit(self.api_root)
        expected_path = urlsplit(self.first_page).path
        if (
            (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc)
            or parsed.path != expected_path
            or parsed.fragment
        ):
            raise MailboxError("Microsoft returned an invalid synchronization link.")
        return value

    def messages(self) -> Iterator[RemoteMessage]:
        cursor = (
            self.sync.cursor_for(self.scope.synchronization_namespace)
            if self.sync is not None
            else (
                self.range_sync.start(self.scope.processing_namespace)
                if self.range_sync is not None
                else None
            )
        )
        for page in self._pages(cursor):
            for message_id in self._page_message_ids(page):
                if message_id in self.seen:
                    continue
                self.seen.add(message_id)
                yield from self._fetch(message_id)
        if self.sync is not None:
            for message_id in sorted(
                self.sync.recheck_ids_for(self.scope.processing_namespace) - self.seen
            ):
                yield from self._fetch(message_id, recheck=True)

    def _pages(self, cursor: str | None) -> Iterator[dict[str, Any]]:
        if cursor is not None:
            cursor = self._trusted_link(cursor)
        page_url: str | None = cursor or self.first_page
        visited: set[str] = set()
        reset_attempted = False
        while page_url:
            if page_url in visited:
                raise MailboxError("Microsoft returned a repeating message continuation link.")
            visited.add(page_url)
            try:
                page = self.http.get_json(page_url, self.headers)
            except ProviderHttpError as exc:
                resettable = exc.status in {404, 410} or (
                    400 <= exc.status < 500
                    and exc.code.casefold() in {"syncstatenotfound", "invaliddeltatoken"}
                )
                if self.range_sync is not None and page_url != self.first_page:
                    if reset_attempted or not resettable:
                        raise
                    self.range_sync.reset()
                    cursor = None
                    self.seen.clear()
                    visited.clear()
                    page_url = self.first_page
                    reset_attempted = True
                    continue
                if cursor is None or not resettable:
                    raise
                assert self.sync is not None
                self.sync.report_reset()
                cursor = None
                self.seen.clear()
                visited.clear()
                page_url = self.first_page
                continue
            yield page
            page_url = self._next_page(page)
            if self.range_sync is not None:
                if page_url is None:
                    self.range_sync.finish()
                else:
                    self.range_sync.advance(page_url)

    def _page_message_ids(self, page: dict[str, Any]) -> Iterator[str]:
        if not isinstance(page.get("value"), list):
            raise MailboxError("Microsoft returned an unexpected message list.")
        for item in _object_list(page, "value", required=True):
            message_id = _nonempty_string(item.get("id"), "Microsoft message ID")
            if "@removed" in item:
                if not isinstance(item["@removed"], dict):
                    raise MailboxError("Microsoft returned an invalid removed message.")
                if self.sync is not None and len(self.target.mailbox.folders) == 1:
                    self.sync.discard(message_id)
                continue
            if self.sync is not None:
                self.sync.mark_present(message_id)
            yield message_id

    def _next_page(self, page: dict[str, Any]) -> str | None:
        next_page = _optional_string(page, "@odata.nextLink")
        next_cursor = _optional_string(page, "@odata.deltaLink")
        if next_page is not None and next_cursor is not None:
            raise MailboxError("Microsoft returned both a continuation and a delta link.")
        if next_page is not None:
            return self._trusted_link(next_page)
        if self.sync is not None:
            if next_cursor is None:
                raise MailboxError("Microsoft did not return a synchronization delta link.")
            self.sync.next_cursor = self._trusted_link(next_cursor)
        return None

    def _folder_id(self, folder: str) -> str:
        if folder not in self.resolved_folders:
            folder_data = self.http.get_json(
                f"{self.api_root}{self.mailbox_root}/mailFolders/{quote(folder, safe='')}?$select=id",
                self.headers,
            )
            folder_id = folder_data.get("id")
            if not isinstance(folder_id, str) or not folder_id:
                raise MailboxError("Microsoft did not return the selected folder ID.")
            self.resolved_folders[folder] = folder_id
        return self.resolved_folders[folder]

    def _matches_parent_folder(self, parent_folder_id: str, *, recheck: bool) -> bool:
        if parent_folder_id == self.resolved_folder_id:
            return True
        if not recheck:
            return False
        if not self.target.mailbox.folders or parent_folder_id in self.resolved_folders.values():
            return True
        selected_folders = self.target.selected_folders or tuple(self.target.mailbox.folders)
        return any(self._folder_id(folder) == parent_folder_id for folder in selected_folders)

    def _fetch(self, message_id: str, *, recheck: bool = False) -> Iterator[RemoteMessage]:
        message_path = quote(message_id, safe="")
        if self.sync is not None and self.sync.baseline:
            self.should_fetch(self.scope, message_id)
            return
        if not self.should_fetch(self.scope, message_id):
            return
        if self.sync is not None and self.resolved_folder_id is None:
            self.resolved_folder_id = self._folder_id(self.folder)
        try:
            if self.sync is not None:
                metadata = self.http.get_json(
                    f"{self.api_root}{self.mailbox_root}/messages/{message_path}?$select=parentFolderId,receivedDateTime",
                    self.headers,
                )
                parent_folder_id = metadata.get("parentFolderId")
                if not isinstance(parent_folder_id, str) or not parent_folder_id:
                    raise RemoteMessageError(
                        "Microsoft did not return the message's parent folder ID."
                    )
                if not self._matches_parent_folder(parent_folder_id, recheck=recheck):
                    if recheck or len(self.target.mailbox.folders) == 1:
                        self.sync.discard(message_id)
                    return
                self.sync.mark_present(message_id)
            else:
                metadata = self.http.get_json(
                    f"{self.api_root}{self.mailbox_root}/messages/{message_path}?$select=receivedDateTime",
                    self.headers,
                )
            timestamp = metadata.get("receivedDateTime")
            if not isinstance(timestamp, str):
                raise RemoteMessageError("Microsoft did not return receivedDateTime.")
            try:
                received = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RemoteMessageError("Microsoft returned an invalid receivedDateTime.") from exc
            if received.tzinfo is None:
                raise RemoteMessageError("Microsoft returned receivedDateTime without a timezone.")
            raw_url = (
                f"{self.api_root}{self.mailbox_root}"
                f"{'/mailFolders/' + self.folder_path if self.sync is not None and not recheck else ''}"
                f"/messages/{message_path}/$value"
            )
            raw_headers = {**self.headers, "Accept": "message/rfc822"}
            raw, raw_chunks = self._message_body(message_id, raw_url, raw_headers)
        except ProviderHttpError as exc:
            if _scan_wide_http_error(exc):
                raise
            if exc.status == 404 and self.sync is not None:
                self.sync.discard(message_id)
                return
            yield RemoteMessage(
                id=message_id, error=_message_http_error("Microsoft", message_id, exc)
            )
            return
        except RemoteMessageError as exc:
            yield RemoteMessage(id=message_id, error=exc)
            return
        yield RemoteMessage(
            id=message_id,
            raw=raw,
            received_at=received.astimezone(timezone.utc),
            received_origin="graph_received_date_time",
            raw_chunks=raw_chunks,
        )

    def _message_body(
        self, message_id: str, raw_url: str, raw_headers: dict[str, str]
    ) -> tuple[bytes | None, Callable[[], Iterator[bytes]] | None]:
        if self.http.supports_streaming:
            return None, self._raw_chunks(
                message_id, self.http.message_chunks(raw_url, raw_headers)
            )
        try:
            return self.http.get_bytes(raw_url, raw_headers), None
        except ProviderHttpError as exc:
            if exc.status != 404:
                raise
            return None, self._unavailable_chunks(message_id)

    def _raw_chunks(
        self, message_id: str, chunks: Callable[[], Iterator[bytes]]
    ) -> Callable[[], Iterator[bytes]]:
        def read() -> Iterator[bytes]:
            try:
                yield from chunks()
            except ProviderHttpError as exc:
                if _scan_wide_http_error(exc):
                    raise ScanWideProviderError(str(exc)) from exc
                raise _message_http_error("Microsoft", message_id, exc) from exc

        return read

    @staticmethod
    def _unavailable_chunks(message_id: str) -> Callable[[], Iterator[bytes]]:
        def read() -> Iterator[bytes]:
            raise RemoteMessageUnavailable(
                f"Microsoft message {message_id} is no longer available."
            )
            yield b""

        return read


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

    def targets(self, account: Account, mailbox: Mailbox) -> list[MailTarget]:
        return self.get(account).targets(account, mailbox)
