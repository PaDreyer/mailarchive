"""Own the loopback listener so a cancelled browser login releases its resources."""

from __future__ import annotations

import hmac
import io
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from mailarchive.application.errors import AuthorizationError


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _RedirectHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(_CallbackReader(self.connection, self.server.receiver))

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            # A browser tab can be closed while the callback is being received.
            pass

    def log_message(self, format, *args) -> None:
        # OAuth codes and state must not be written to access logs.
        pass

    def do_GET(self) -> None:
        self._receive(urlsplit(self.path).query)

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < size <= 16384
                or self.headers.get_content_type() != "application/x-www-form-urlencoded"
            ):
                raise ValueError("Invalid authorization response.")
            self._receive(self.rfile.read(size).decode("utf-8"))
        except (UnicodeError, ValueError):
            self._respond(400, "Invalid authorization response.")

    def _receive(self, query: str) -> None:
        receiver = self.server.receiver
        try:
            values = parse_qs(query, max_num_fields=20)
            response = {key: value[0] for key, value in values.items() if len(value) == 1}
        except ValueError:
            response = {}
        if (
            urlsplit(self.path).path != "/"
            or not hmac.compare_digest(
                response.get("state", "").encode("utf-8"), receiver.state.encode("utf-8")
            )
            or not (response.get("code") or response.get("error"))
        ):
            self._respond(400, "Invalid authorization response.")
            return
        receiver.response = response
        self._respond(
            200,
            "Sign-in received. Return to MailArchive to see the result. You can close this tab.",
        )

    def _respond(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class _CallbackReader(io.RawIOBase):
    """Bound the entire request and check cancellation between received chunks."""

    def __init__(self, connection: socket.socket, receiver: BrowserAuthorization) -> None:
        self.connection, self.receiver = connection, receiver
        self.deadline = min(time.monotonic() + 2, receiver.deadline)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or (self.receiver.cancelled and self.receiver.cancelled.is_set()):
                raise TimeoutError("Authorization callback interrupted.")
            self.connection.settimeout(min(0.1, remaining))
            try:
                return self.connection.recv_into(buffer)
            except TimeoutError:
                continue


class BrowserAuthorization:
    """A receiver usable by MSAL and Google's authorization-code flow.

    The application's existing worker handles requests itself. There is no detached
    browser-wait thread to survive cancellation or closure of the account dialog.
    """

    def __init__(self, cancelled: threading.Event | None) -> None:
        self.cancelled = cancelled
        self.state = ""
        self.response: dict[str, str] | None = None
        self.deadline = 0.0
        self._check_cancelled()
        self._server = _LoopbackServer(("127.0.0.1", 0), _RedirectHandler)
        self._server.receiver = self
        self._server.timeout = 0.1

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self._server.server_port}"

    def get_port(self) -> int:
        return self._server.server_port

    def _check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled.is_set():
            raise AuthorizationError("Authorization cancelled.")

    def get_auth_response(
        self,
        *,
        auth_uri: str,
        state: str,
        timeout: float,
        browser_name: str | None = None,
        **kwargs,
    ) -> dict[str, str] | None:
        self.state = state
        self.deadline = time.monotonic() + timeout
        self._check_cancelled()
        try:
            if not webbrowser.get(browser_name).open(auth_uri, new=1, autoraise=True):
                raise AuthorizationError("The system browser could not be opened.")
        except webbrowser.Error as exc:
            raise AuthorizationError("The system browser could not be opened.") from exc
        while self.response is None:
            self._check_cancelled()
            if time.monotonic() >= self.deadline:
                return None
            self._server.handle_request()
        self._check_cancelled()
        return self.response

    def __enter__(self) -> BrowserAuthorization:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._server.server_close()
