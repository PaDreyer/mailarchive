from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterator
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mailarchive.application.intake_limits import (
    MAX_GMAIL_WIRE_BYTES,
    MAX_JSON_BYTES,
    MAX_MESSAGE_BYTES,
    MESSAGE_CHUNK_BYTES,
    MessageTooLargeError,
)
from mailarchive.application.source_port import (
    MailboxError,
    RemoteMessageError,
    RemoteMessageUnavailable,
)

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
