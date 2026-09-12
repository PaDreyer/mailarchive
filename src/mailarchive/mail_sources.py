from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from mailarchive.credential_data import load_credential_data
from mailarchive.credentials import CredentialStore
from mailarchive.imap_client import ImapMailbox, MailboxError, RemoteMessage
from mailarchive.models import Account, AuthMode, MailProvider
from mailarchive.oauth import OAuthManager

MessageFilter = Callable[[str, str], bool]


class MessageSource(Protocol):
    def fetch_messages(
        self,
        account: Account,
        should_fetch: MessageFilter,
    ) -> tuple[str, Iterator[RemoteMessage]]: ...


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
            raise MailboxError(
                f"The mail provider returned HTTP {exc.code}: {detail[:500]}"
            ) from exc
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

    def fetch_messages(
        self,
        account: Account,
        should_fetch: MessageFilter,
    ) -> tuple[str, Iterator[RemoteMessage]]:
        def imap_filter(uid_validity: str, uid: str) -> bool:
            return should_fetch(f"imap:{uid_validity}", uid)

        if account.auth_mode == AuthMode.PASSWORD:
            data = load_credential_data(self.credential_store, account.id)
            password = str(data.get("password", ""))
            if not password:
                raise MailboxError("No password is stored. Edit the email account to add one.")
            uid_validity, messages = self.mailbox.fetch_messages(
                account,
                password,
                imap_filter,
            )
        elif account.auth_mode == AuthMode.OAUTH_USER:
            access_token = self.oauth.microsoft_access_token(account)
            uid_validity, messages = self.mailbox.fetch_messages(
                account,
                None,
                imap_filter,
                access_token=access_token,
            )
        else:
            raise MailboxError("Generic IMAP does not support application authentication.")
        return f"imap:{uid_validity}", messages


class GmailMessageSource:
    API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, oauth: OAuthManager, http: HttpClient | None = None) -> None:
        self.oauth = oauth
        self.http = http or HttpClient()

    def fetch_messages(
        self,
        account: Account,
        should_fetch: MessageFilter,
    ) -> tuple[str, Iterator[RemoteMessage]]:
        access_token = self.oauth.google_access_token(account)
        label = account.folder.strip() or "INBOX"
        namespace = f"gmail-api:{label}"

        def iterator() -> Iterator[RemoteMessage]:
            page_token: str | None = None
            while True:
                parameters = {"labelIds": label, "maxResults": "500"}
                if page_token:
                    parameters["pageToken"] = page_token
                page = self.http.get_json(
                    f"{self.API_ROOT}/messages?{urlencode(parameters)}",
                    access_token,
                )
                for item in page.get("messages", []):
                    message_id = str(item.get("id", ""))
                    if not message_id or not should_fetch(namespace, message_id):
                        continue
                    message = self.http.get_json(
                        f"{self.API_ROOT}/messages/{quote(message_id, safe='')}?format=raw&fields=raw",
                        access_token,
                    )
                    encoded = str(message.get("raw", ""))
                    if not encoded:
                        raise MailboxError(f"Gmail message {message_id} did not contain MIME data.")
                    padding = "=" * (-len(encoded) % 4)
                    try:
                        raw = base64.urlsafe_b64decode(encoded + padding)
                    except ValueError as exc:
                        raise MailboxError(
                            f"Gmail message {message_id} contained invalid MIME data."
                        ) from exc
                    yield RemoteMessage(id=message_id, raw=raw)
                next_page = page.get("nextPageToken")
                if not next_page:
                    break
                page_token = str(next_page)

        return namespace, iterator()


class MicrosoftGraphMessageSource:
    API_ROOT = "https://graph.microsoft.com/v1.0"
    GRAPH_HEADERS = {"Prefer": 'IdType="ImmutableId"'}

    def __init__(self, oauth: OAuthManager, http: HttpClient | None = None) -> None:
        self.oauth = oauth
        self.http = http or HttpClient()

    def fetch_messages(
        self,
        account: Account,
        should_fetch: MessageFilter,
    ) -> tuple[str, Iterator[RemoteMessage]]:
        access_token = self.oauth.microsoft_access_token(account)
        folder = account.folder.strip() or "inbox"
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            mailbox_root = f"/users/{quote(account.username, safe='')}"
        else:
            mailbox_root = "/me"
        folder_path = quote(folder, safe="")
        namespace = f"microsoft-graph:{folder}"
        parameters = urlencode({"$select": "id", "$top": "999"})
        first_page = (
            f"{self.API_ROOT}{mailbox_root}/mailFolders/{folder_path}/messages?{parameters}"
        )

        def iterator() -> Iterator[RemoteMessage]:
            page_url: str | None = first_page
            while page_url:
                page = self.http.get_json(
                    page_url,
                    access_token,
                    self.GRAPH_HEADERS,
                )
                for item in page.get("value", []):
                    message_id = str(item.get("id", ""))
                    if not message_id or not should_fetch(namespace, message_id):
                        continue
                    raw = self.http.get_bytes(
                        f"{self.API_ROOT}{mailbox_root}/messages/{quote(message_id, safe='')}/$value",
                        access_token,
                        {**self.GRAPH_HEADERS, "Accept": "message/rfc822"},
                    )
                    yield RemoteMessage(id=message_id, raw=raw)
                next_page = page.get("@odata.nextLink")
                page_url = str(next_page) if next_page else None

        return namespace, iterator()


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
