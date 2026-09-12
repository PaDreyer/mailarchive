from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from mailarchive.credential_data import load_credential_data
from mailarchive.credentials import CredentialStore
from mailarchive.imap_client import ImapMailbox, MailboxError, RemoteMessage
from mailarchive.mail_identity import MailTarget, MessageScope, api_scope
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from mailarchive.oauth import OAuthManager
from mailarchive.synchronization import SyncSession

MessageFilter = Callable[[MessageScope, str], bool]


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
        raise MailboxError("Gmail returned invalid label IDs.")
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


class HttpClient:
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
        request_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "MailArchive/0.1",
        }
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers)
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            raise ProviderHttpError(exc.code, detail) from exc
        except (OSError, URLError) as exc:
            raise MailboxError(str(exc)) from exc


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
            target, access_token=self.oauth.microsoft_access_token(account)
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
                sync=sync,
            )
        else:
            raise MailboxError("Generic IMAP does not support application authentication.")
        return scope, messages


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


class _GmailMailboxScan:
    """Own one mailbox enumeration, including pagination, downloads, and rechecks."""

    def __init__(
        self,
        source: GmailMessageSource,
        target: MailTarget,
        access_token: str,
        should_fetch: MessageFilter,
        sync: SyncSession | None,
    ) -> None:
        self.http = source.http
        self.api_root = f"{source.API_ROOT}/{quote(target.mailbox.address.strip(), safe='')}"
        self.access_token = access_token
        self.should_fetch = should_fetch
        self.sync = sync
        self.labels = set(target.mailbox.folders)
        self.scope = api_scope(target)
        self.seen: set[str] = set()

    def _selected(self, message_labels: list[str]) -> bool:
        return not self.labels or bool(self.labels.intersection(message_labels))

    def _full_ids(self) -> Iterator[str]:
        if self.sync is not None:
            profile = self.http.get_json(
                f"{self.api_root}/profile?fields=historyId", self.access_token
            )
            cursor = _history_id(profile.get("historyId"))
        for label in sorted(self.labels) or [""]:
            page_token: str | None = None
            visited: set[str] = set()
            while True:
                parameters = {"labelIds": label} if label else {}
                parameters.update({"maxResults": "500", "includeSpamTrash": "true"})
                if page_token:
                    parameters["pageToken"] = page_token
                page = self.http.get_json(
                    f"{self.api_root}/messages?{urlencode(parameters)}",
                    self.access_token,
                )
                for item in _object_list(page, "messages"):
                    message_id = _nonempty_string(item.get("id"), "Gmail message ID")
                    if self.sync is not None:
                        self.sync.mark_present(message_id)
                    yield message_id
                page_token = _optional_string(page, "nextPageToken")
                if page_token is None:
                    break
                if page_token in visited:
                    raise MailboxError("Gmail returned a repeating message page token.")
                visited.add(page_token)
        if self.sync is not None:
            # Capture before listing: changes during the full scan are replayed next time.
            self.sync.next_cursor = cursor

    def _history_ids(self, cursor: str) -> Iterator[str]:
        _history_id(cursor)
        page_token: str | None = None
        visited: set[str] = set()
        while True:
            parameters = {"startHistoryId": cursor, "maxResults": "500"}
            if page_token:
                parameters["pageToken"] = page_token
            try:
                page = self.http.get_json(
                    f"{self.api_root}/history?{urlencode(parameters)}", self.access_token
                )
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
                if "labelIds" not in message or self._selected(_label_ids(message)):
                    yield message_id
            for added in _object_list(history, "labelsAdded"):
                message = added.get("message")
                if not isinstance(message, dict):
                    raise MailboxError("Gmail returned an invalid label change message.")
                message_id = _nonempty_string(message.get("id"), "Gmail message ID")
                if self._selected(_label_ids(added)):
                    yield message_id

    def _fetch(self, message_id: str, verify_label: bool) -> Iterator[RemoteMessage]:
        message_url = f"{self.api_root}/messages/{quote(message_id, safe='')}"
        try:
            if verify_label:
                metadata = self.http.get_json(
                    f"{message_url}?format=minimal&fields=labelIds", self.access_token
                )
                if not self._selected(_label_ids(metadata)):
                    assert self.sync is not None
                    self.sync.discarded_ids.add(message_id)
                    return
                assert self.sync is not None
                self.sync.mark_present(message_id)
            if not self.should_fetch(self.scope, message_id):
                return
            fields = "raw,labelIds" if self.sync is not None else "raw"
            message = self.http.get_json(
                f"{message_url}?format=raw&fields={fields}", self.access_token
            )
        except ProviderHttpError as exc:
            if self.sync is None or exc.status != 404:
                raise
            self.sync.discarded_ids.add(message_id)
            return
        if self.sync is not None and not self._selected(_label_ids(message)):
            self.sync.discarded_ids.add(message_id)
            return
        encoded = message.get("raw")
        if not isinstance(encoded, str) or not encoded:
            raise MailboxError(f"Gmail message {message_id} did not contain MIME data.")
        padding = "=" * (-len(encoded) % 4)
        try:
            raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        except ValueError as exc:
            raise MailboxError(f"Gmail message {message_id} contained invalid MIME data.") from exc
        yield RemoteMessage(id=message_id, raw=raw)

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
        token = self.oauth.microsoft_access_token(account)
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
            page = self.http.get_json(url, token, self.GRAPH_HEADERS)
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


class _GraphFolderScan:
    """Own one folder delta scan and its mailbox-wide targeted rechecks."""

    def __init__(
        self,
        source: MicrosoftGraphMessageSource,
        target: MailTarget,
        access_token: str,
        should_fetch: MessageFilter,
        sync: SyncSession | None,
    ) -> None:
        self.http = source.http
        self.api_root = source.API_ROOT
        self.headers = source.GRAPH_HEADERS
        self.target = target
        self.access_token = access_token
        self.should_fetch = should_fetch
        self.sync = sync
        self.folder = target.folder.strip() or "inbox"
        self.folder_path = quote(self.folder, safe="")
        self.mailbox_root = source._mailbox_root(target)
        self.scope = api_scope(target)
        parameters = urlencode({"$select": "id", "$top": "999"})
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
        if (
            (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc)
            or not parsed.path.startswith(root.path + "/")
            or parsed.fragment
        ):
            raise MailboxError("Microsoft returned an invalid synchronization link.")
        return value

    def messages(self) -> Iterator[RemoteMessage]:
        cursor = (
            self.sync.cursor_for(self.scope.synchronization_namespace)
            if self.sync is not None
            else None
        )
        for page in self._pages(cursor):
            for message_id in self._page_message_ids(page):
                if message_id in self.seen:
                    continue
                self.seen.add(message_id)
                yield from self._fetch(message_id)
                if self.sync is not None and message_id in self.sync.discarded_ids:
                    self.seen.discard(message_id)
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
        while page_url:
            if page_url in visited:
                raise MailboxError("Microsoft returned a repeating message continuation link.")
            visited.add(page_url)
            try:
                page = self.http.get_json(page_url, self.access_token, self.headers)
            except ProviderHttpError as exc:
                if cursor is None or not (
                    exc.status in {404, 410}
                    or (
                        400 <= exc.status < 500
                        and exc.code.casefold() in {"syncstatenotfound", "invaliddeltatoken"}
                    )
                ):
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

    def _page_message_ids(self, page: dict[str, Any]) -> Iterator[str]:
        if not isinstance(page.get("value"), list):
            raise MailboxError("Microsoft returned an unexpected message list.")
        for item in _object_list(page, "value", required=True):
            message_id = _nonempty_string(item.get("id"), "Microsoft message ID")
            if "@removed" in item:
                if not isinstance(item["@removed"], dict):
                    raise MailboxError("Microsoft returned an invalid removed message.")
                if self.sync is not None and len(self.target.mailbox.folders) == 1:
                    self.sync.discarded_ids.add(message_id)
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
                self.access_token,
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
        if not self.should_fetch(self.scope, message_id):
            return
        if self.sync is not None and self.resolved_folder_id is None:
            self.resolved_folder_id = self._folder_id(self.folder)
        try:
            if self.sync is not None:
                metadata = self.http.get_json(
                    f"{self.api_root}{self.mailbox_root}/messages/{message_path}?$select=parentFolderId",
                    self.access_token,
                    self.headers,
                )
                parent_folder_id = metadata.get("parentFolderId")
                if not isinstance(parent_folder_id, str) or not parent_folder_id:
                    raise MailboxError("Microsoft did not return the message's parent folder ID.")
                if not self._matches_parent_folder(parent_folder_id, recheck=recheck):
                    if recheck or len(self.target.mailbox.folders) == 1:
                        self.sync.discarded_ids.add(message_id)
                    return
                self.sync.mark_present(message_id)
            raw = self.http.get_bytes(
                f"{self.api_root}{self.mailbox_root}"
                f"{'/mailFolders/' + self.folder_path if self.sync is not None and not recheck else ''}"
                f"/messages/{message_path}/$value",
                self.access_token,
                {**self.headers, "Accept": "message/rfc822"},
            )
        except ProviderHttpError as exc:
            if self.sync is None or exc.status != 404:
                raise
            self.sync.discarded_ids.add(message_id)
            return
        yield RemoteMessage(id=message_id, raw=raw)


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
