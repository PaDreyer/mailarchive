from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation
from mailarchive.application.source_port import (
    MailboxError,
    MessageFilter,
    RemoteMessage,
    RemoteMessageError,
    RemoteMessageUnavailable,
    ScanWideProviderError,
)
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import Account, AuthMode, Mailbox
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


class MicrosoftGraphMessageSource:
    API_ROOT = "https://graph.microsoft.com/v1.0"
    GRAPH_HEADERS = {"Prefer": 'IdType="ImmutableId"'}

    def __init__(self, oauth: OAuthManager, http: HttpClient | None = None) -> None:
        self.oauth = oauth
        self.http = http or HttpClient()

    def targets(
        self, account: Account, mailbox: Mailbox, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[MailTarget]:
        folders = mailbox.folders or self.list_folders(
            MailTarget(account, mailbox, ""), cancellation=cancellation
        )
        return [MailTarget(account, mailbox, folder, tuple(folders)) for folder in folders]

    def list_folders(
        self, target: MailTarget, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[str]:
        cancellation.checkpoint()
        account = target.account
        http = _OAuthHttpSession(
            self.http,
            self.oauth.microsoft_access_token(account),
            lambda: self.oauth.microsoft_access_token(account, force_refresh=True),
            cancellation=cancellation,
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
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]:
        cancellation.checkpoint()
        access_token = self.oauth.microsoft_access_token(target.account)
        scan = _GraphFolderScan(
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
        token = self.oauth.microsoft_access_token(target.account)
        scan = _GraphFolderScan(
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
        scan = _GraphFolderScan(
            self,
            target,
            self.oauth.microsoft_access_token(target.account),
            lambda _scope, _remote_id: True,
            None,
            cancellation=cancellation,
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
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        self.cancellation = cancellation
        self.http = _OAuthHttpSession(
            source.http,
            access_token,
            lambda: source.oauth.microsoft_access_token(target.account, force_refresh=True),
            cancellation=cancellation,
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
        if (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc) or parsed.fragment:
            raise MailboxError("Microsoft returned an invalid synchronization link.")
        # Graph can replace folder aliases with IDs and use OData key syntax.
        folder_path = re.fullmatch(
            re.escape(f"{root.path}{self.mailbox_root}")
            + r"/mailfolders(?:/([^/]+)|\('((?:[^']|'')+)'\))/messages"
            + ("/delta" if self.sync is not None else ""),
            parsed.path,
            flags=re.IGNORECASE,
        )
        if folder_path is None:
            raise MailboxError("Microsoft returned an invalid synchronization link.")
        folder = (
            unquote(folder_path[1])
            if folder_path[1] is not None
            else unquote(folder_path[2]).replace("''", "'")
        )
        if folder != self.folder and folder != self._folder_id(self.folder):
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
        self.cancellation.checkpoint()
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
