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
        account = target.account
        access_token = self.oauth.google_access_token(
            account, mailbox_address=target.mailbox.address
        )
        api_root = f"{self.API_ROOT}/{quote(target.mailbox.address.strip(), safe='')}"
        labels = set(target.mailbox.folders)
        scope = api_scope(target)
        seen: set[str] = set()

        def selected(message_labels: list[str]) -> bool:
            return not labels or bool(labels.intersection(message_labels))

        def full_ids() -> Iterator[str]:
            if sync is not None:
                profile = self.http.get_json(f"{api_root}/profile?fields=historyId", access_token)
                cursor = _history_id(profile.get("historyId"))
            for label in sorted(labels) or [""]:
                page_token: str | None = None
                visited: set[str] = set()
                while True:
                    parameters = {"labelIds": label} if label else {}
                    parameters.update({"maxResults": "500", "includeSpamTrash": "true"})
                    if page_token:
                        parameters["pageToken"] = page_token
                    page = self.http.get_json(
                        f"{api_root}/messages?{urlencode(parameters)}",
                        access_token,
                    )
                    for item in _object_list(page, "messages"):
                        message_id = _nonempty_string(item.get("id"), "Gmail message ID")
                        if sync is not None:
                            sync.mark_present(message_id)
                        yield message_id
                    page_token = _optional_string(page, "nextPageToken")
                    if page_token is None:
                        break
                    if page_token in visited:
                        raise MailboxError("Gmail returned a repeating message page token.")
                    visited.add(page_token)
            if sync is not None:
                # Capture before listing: changes during the full scan are replayed next time.
                sync.next_cursor = cursor

        def history_ids(cursor: str) -> Iterator[str]:
            _history_id(cursor)
            page_token: str | None = None
            visited: set[str] = set()
            while True:
                parameters = {"startHistoryId": cursor, "maxResults": "500"}
                if page_token:
                    parameters["pageToken"] = page_token
                try:
                    page = self.http.get_json(
                        f"{api_root}/history?{urlencode(parameters)}", access_token
                    )
                except ProviderHttpError as exc:
                    if exc.status != 404:
                        raise
                    assert sync is not None
                    sync.report_reset()
                    seen.clear()
                    yield from full_ids()
                    return
                next_cursor = _history_id(page.get("historyId"))
                normalized_next = next_cursor.lstrip("0")
                normalized_start = cursor.lstrip("0")
                if (len(normalized_next), normalized_next) < (
                    len(normalized_start),
                    normalized_start,
                ):
                    raise MailboxError("Gmail returned a history ID older than the stored cursor.")
                for history in _object_list(page, "history"):
                    for added in _object_list(history, "messagesAdded"):
                        message = added.get("message")
                        if not isinstance(message, dict):
                            raise MailboxError("Gmail returned an invalid added message.")
                        message_id = _nonempty_string(message.get("id"), "Gmail message ID")
                        if "labelIds" not in message or selected(_label_ids(message)):
                            yield message_id
                    for added in _object_list(history, "labelsAdded"):
                        message = added.get("message")
                        if not isinstance(message, dict):
                            raise MailboxError("Gmail returned an invalid label change message.")
                        message_id = _nonempty_string(message.get("id"), "Gmail message ID")
                        if selected(_label_ids(added)):
                            yield message_id
                page_token = _optional_string(page, "nextPageToken")
                if page_token is None:
                    assert sync is not None
                    sync.next_cursor = next_cursor
                    return
                if page_token in visited:
                    raise MailboxError("Gmail returned a repeating history page token.")
                visited.add(page_token)

        def fetch(message_id: str, verify_label: bool) -> Iterator[RemoteMessage]:
            message_url = f"{api_root}/messages/{quote(message_id, safe='')}"
            try:
                if verify_label:
                    metadata = self.http.get_json(
                        f"{message_url}?format=minimal&fields=labelIds", access_token
                    )
                    if not selected(_label_ids(metadata)):
                        assert sync is not None
                        sync.discarded_ids.add(message_id)
                        return
                    assert sync is not None
                    sync.mark_present(message_id)
                if not should_fetch(scope, message_id):
                    return
                fields = "raw,labelIds" if sync is not None else "raw"
                message = self.http.get_json(
                    f"{message_url}?format=raw&fields={fields}", access_token
                )
            except ProviderHttpError as exc:
                if sync is None or exc.status != 404:
                    raise
                sync.discarded_ids.add(message_id)
                return
            if sync is not None and not selected(_label_ids(message)):
                sync.discarded_ids.add(message_id)
                return
            encoded = message.get("raw")
            if not isinstance(encoded, str) or not encoded:
                raise MailboxError(f"Gmail message {message_id} did not contain MIME data.")
            padding = "=" * (-len(encoded) % 4)
            try:
                raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
            except ValueError as exc:
                raise MailboxError(
                    f"Gmail message {message_id} contained invalid MIME data."
                ) from exc
            yield RemoteMessage(id=message_id, raw=raw)

        def iterator() -> Iterator[RemoteMessage]:
            cursor = sync.cursor_for(scope.synchronization_namespace) if sync is not None else None
            ids = history_ids(cursor) if cursor is not None else full_ids()
            for message_id in ids:
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    yield from fetch(message_id, verify_label=cursor is not None)
                    if sync is not None and message_id in sync.discarded_ids:
                        seen.discard(message_id)
            if sync is not None:
                for message_id in sorted(sync.recheck_ids_for(scope.processing_namespace) - seen):
                    yield from fetch(message_id, verify_label=True)

        return scope, iterator()


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
        account = target.account
        access_token = self.oauth.microsoft_access_token(account)
        folder = target.folder.strip() or "inbox"
        mailbox_root = self._mailbox_root(target)
        folder_path = quote(folder, safe="")
        scope = api_scope(target)
        parameters = urlencode({"$select": "id", "$top": "999"})
        first_page = (
            f"{self.API_ROOT}{mailbox_root}/mailFolders/{folder_path}/messages"
            f"{'/delta' if sync is not None else ''}?{parameters}"
        )
        resolved_folder_id: str | None = None
        resolved_folders: dict[str, str] = {}

        def trusted_link(value: str) -> str:
            parsed = urlsplit(value)
            root = urlsplit(self.API_ROOT)
            if (
                (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc)
                or not parsed.path.startswith(root.path + "/")
                or parsed.fragment
            ):
                raise MailboxError("Microsoft returned an invalid synchronization link.")
            return value

        def iterator() -> Iterator[RemoteMessage]:
            cursor = sync.cursor_for(scope.synchronization_namespace) if sync is not None else None
            if cursor is not None:
                cursor = trusted_link(cursor)
            page_url: str | None = cursor or first_page
            seen: set[str] = set()
            visited: set[str] = set()
            while page_url:
                if page_url in visited:
                    raise MailboxError("Microsoft returned a repeating message continuation link.")
                visited.add(page_url)
                try:
                    page = self.http.get_json(page_url, access_token, self.GRAPH_HEADERS)
                except ProviderHttpError as exc:
                    if cursor is None or not (
                        exc.status in {404, 410}
                        or (
                            400 <= exc.status < 500
                            and exc.code.casefold() in {"syncstatenotfound", "invaliddeltatoken"}
                        )
                    ):
                        raise
                    assert sync is not None
                    sync.report_reset()
                    cursor = None
                    seen.clear()
                    visited.clear()
                    page_url = first_page
                    continue
                if not isinstance(page.get("value"), list):
                    raise MailboxError("Microsoft returned an unexpected message list.")
                for item in _object_list(page, "value", required=True):
                    message_id = _nonempty_string(item.get("id"), "Microsoft message ID")
                    if "@removed" in item:
                        if not isinstance(item["@removed"], dict):
                            raise MailboxError("Microsoft returned an invalid removed message.")
                        if sync is not None and len(target.mailbox.folders) == 1:
                            sync.discarded_ids.add(message_id)
                        continue
                    if sync is not None:
                        sync.mark_present(message_id)
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    yield from fetch(message_id)
                    if sync is not None and message_id in sync.discarded_ids:
                        seen.discard(message_id)
                next_page = _optional_string(page, "@odata.nextLink")
                next_cursor = _optional_string(page, "@odata.deltaLink")
                if next_page is not None and next_cursor is not None:
                    raise MailboxError("Microsoft returned both a continuation and a delta link.")
                page_url = trusted_link(next_page) if next_page is not None else None
                if page_url is None and sync is not None:
                    if next_cursor is None:
                        raise MailboxError("Microsoft did not return a synchronization delta link.")
                    sync.next_cursor = trusted_link(next_cursor)
            if sync is not None:
                for message_id in sorted(sync.recheck_ids_for(scope.processing_namespace) - seen):
                    yield from fetch(message_id, recheck=True)

        def fetch(message_id: str, *, recheck: bool = False) -> Iterator[RemoteMessage]:
            nonlocal resolved_folder_id
            message_path = quote(message_id, safe="")
            if not should_fetch(scope, message_id):
                return
            if sync is not None and resolved_folder_id is None:
                folder_data = self.http.get_json(
                    f"{self.API_ROOT}{mailbox_root}/mailFolders/{folder_path}?$select=id",
                    access_token,
                    self.GRAPH_HEADERS,
                )
                resolved_folder_id = folder_data.get("id")
                if not isinstance(resolved_folder_id, str) or not resolved_folder_id:
                    raise MailboxError("Microsoft did not return the selected folder ID.")
                resolved_folders[folder] = resolved_folder_id
            try:
                if sync is not None:
                    metadata = self.http.get_json(
                        f"{self.API_ROOT}{mailbox_root}/messages/{message_path}?$select=parentFolderId",
                        access_token,
                        self.GRAPH_HEADERS,
                    )
                    parent_folder_id = metadata.get("parentFolderId")
                    if not isinstance(parent_folder_id, str) or not parent_folder_id:
                        raise MailboxError(
                            "Microsoft did not return the message's parent folder ID."
                        )
                    if parent_folder_id != resolved_folder_id:
                        if recheck and (
                            not target.mailbox.folders
                            or parent_folder_id in resolved_folders.values()
                        ):
                            pass
                        elif recheck:
                            for selected_folder in target.selected_folders or tuple(
                                target.mailbox.folders
                            ):
                                if selected_folder not in resolved_folders:
                                    data = self.http.get_json(
                                        f"{self.API_ROOT}{mailbox_root}/mailFolders/{quote(selected_folder, safe='')}?$select=id",
                                        access_token,
                                        self.GRAPH_HEADERS,
                                    )
                                    selected_id = data.get("id")
                                    if not isinstance(selected_id, str) or not selected_id:
                                        raise MailboxError(
                                            "Microsoft did not return the selected folder ID."
                                        )
                                    resolved_folders[selected_folder] = selected_id
                                if resolved_folders[selected_folder] == parent_folder_id:
                                    break
                            else:
                                sync.discarded_ids.add(message_id)
                                return
                        else:
                            if len(target.mailbox.folders) == 1:
                                sync.discarded_ids.add(message_id)
                            return
                    sync.mark_present(message_id)
                raw = self.http.get_bytes(
                    f"{self.API_ROOT}{mailbox_root}"
                    f"{'/mailFolders/' + folder_path if sync is not None and not recheck else ''}"
                    f"/messages/{message_path}/$value",
                    access_token,
                    {**self.GRAPH_HEADERS, "Accept": "message/rfc822"},
                )
            except ProviderHttpError as exc:
                if sync is None or exc.status != 404:
                    raise
                sync.discarded_ids.add(message_id)
                return
            yield RemoteMessage(id=message_id, raw=raw)

        return scope, iterator()


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
