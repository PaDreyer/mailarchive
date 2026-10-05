"""Real loopback callbacks and cancellation without contacting an OAuth provider."""

import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from msal.oauth2cli.oauth2 import Client

from mailarchive.application.errors import AuthorizationError
from mailarchive.infrastructure.browser_authorization import BrowserAuthorization
from tests.concurrency import THREAD_TIMEOUT


class BrowserAuthorizationTests(unittest.TestCase):
    @contextmanager
    def browser_wait(self, receiver, work=None):
        entered = threading.Event()
        browser = Mock()
        browser.open.side_effect = lambda *args, **kwargs: entered.set() or True

        def run():
            with receiver:
                return (
                    work()
                    if work
                    else receiver.get_auth_response(
                        auth_uri="https://example.org/authorize", state="expected", timeout=5
                    )
                )

        with (
            patch(
                "mailarchive.infrastructure.browser_authorization.webbrowser.get",
                return_value=browser,
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(run)
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                yield pending, browser.open.call_args.args[0]
            finally:
                receiver.cancelled.set()

    def callback(self, receiver, values, *, method="GET", path="/", content_type=None):
        connection = HTTPConnection("127.0.0.1", receiver.get_port(), timeout=2)
        try:
            query = urlencode(values, doseq=True)
            if method == "GET":
                connection.request(method, path + "?" + query)
            else:
                connection.request(
                    method,
                    path,
                    body=query,
                    headers={"Content-Type": content_type or "application/x-www-form-urlencoded"},
                )
            response = connection.getresponse()
            status = response.status
            self.assertNotIn("synthetic-code", response.read().decode())
            return status
        finally:
            connection.close()

    def test_query_and_form_callbacks_validate_state_and_release_the_listener(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                receiver = BrowserAuthorization(threading.Event())
                with self.browser_wait(receiver) as (pending, _):
                    for values in (
                        {"code": "synthetic-code", "state": "wrong"},
                        {"code": "synthetic-code", "state": "non-ascii-ä"},
                        {"code": "synthetic-code", "state": ["expected", "extra"]},
                        {"state": "expected"},
                    ):
                        self.assertEqual(self.callback(receiver, values, method=method), 400)
                        self.assertFalse(pending.done())
                    values = {"code": "synthetic-code", "state": "expected"}
                    self.assertEqual(self.callback(receiver, values, method=method), 200)
                    self.assertEqual(pending.result(THREAD_TIMEOUT), values)
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", receiver.get_port()), timeout=1)

    def test_denied_consent_returns_provider_error_without_credentials(self):
        receiver = BrowserAuthorization(threading.Event())
        with self.browser_wait(receiver) as (pending, _):
            values = {"error": "access_denied", "state": "expected"}
            self.assertEqual(self.callback(receiver, values), 200)
            self.assertEqual(pending.result(THREAD_TIMEOUT), values)

    def test_timeout_and_browser_failure_close_the_listener(self):
        for opened in (True, False):
            with self.subTest(browser_opened=opened):
                receiver = BrowserAuthorization(None)
                with patch(
                    "mailarchive.infrastructure.browser_authorization.webbrowser.get"
                ) as browser:
                    browser.return_value.open.return_value = opened
                    with receiver:
                        if opened:
                            self.assertIsNone(
                                receiver.get_auth_response(
                                    auth_uri="https://example.org", state="expected", timeout=0.01
                                )
                            )
                        else:
                            with self.assertRaisesRegex(AuthorizationError, "could not be opened"):
                                receiver.get_auth_response(
                                    auth_uri="https://example.org", state="expected", timeout=5
                                )
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", receiver.get_port()), timeout=1)

    def test_cancellation_stops_waiting_even_with_an_incomplete_request(self):
        receiver = BrowserAuthorization(threading.Event())
        with self.browser_wait(receiver) as (pending, _):
            with socket.create_connection(("127.0.0.1", receiver.get_port()), timeout=1) as request:
                request.sendall(b"GET / HTTP/1.1\r\n")
                receiver.cancelled.set()
                with self.assertRaisesRegex(AuthorizationError, "cancelled"):
                    pending.result(THREAD_TIMEOUT)

    def test_cancellation_interrupts_a_callback_that_keeps_sending_partial_headers(self):
        receiver = BrowserAuthorization(threading.Event())
        with self.browser_wait(receiver) as (pending, _):
            stop, sending = threading.Event(), threading.Event()
            with socket.create_connection(("127.0.0.1", receiver.get_port()), timeout=1) as request:
                request.sendall(b"GET / HTTP/1.1\r\nX-Slow: ")

                def send_headers():
                    try:
                        while not stop.is_set():
                            request.sendall(b"x")
                            sending.set()
                            stop.wait(0.05)
                    except OSError:
                        pass

                sender = threading.Thread(target=send_headers)
                sender.start()
                try:
                    self.assertTrue(sending.wait(THREAD_TIMEOUT))
                    receiver.cancelled.set()
                    with self.assertRaisesRegex(AuthorizationError, "cancelled"):
                        pending.result(timeout=1)
                finally:
                    stop.set()
                    sender.join(THREAD_TIMEOUT)

    def test_real_msal_client_uses_owned_receiver_for_form_post_and_pkce(self):
        receiver = BrowserAuthorization(threading.Event())
        client = Client(
            {
                "authorization_endpoint": "https://example.org/authorize",
                "token_endpoint": "https://example.org/token",
            },
            "synthetic-client",
        )
        with patch.object(
            client, "obtain_token_by_auth_code_flow", return_value={"access_token": "synthetic"}
        ) as exchange:
            with self.browser_wait(
                receiver,
                lambda: client.obtain_token_by_browser(
                    auth_code_receiver=receiver,
                    redirect_uri=receiver.redirect_uri,
                    scope=["Mail.Read"],
                    timeout=5,
                ),
            ) as (pending, auth_uri):
                parameters = parse_qs(urlsplit(auth_uri).query)
                self.assertEqual(parameters["response_mode"], ["form_post"])
                self.assertEqual(parameters["code_challenge_method"], ["S256"])
                self.assertEqual(parameters["redirect_uri"], [receiver.redirect_uri])
                response = {"state": parameters["state"][0], "code": "synthetic-code"}
                self.assertEqual(self.callback(receiver, response, method="POST"), 200)
                self.assertEqual(pending.result(THREAD_TIMEOUT), {"access_token": "synthetic"})
            self.assertEqual(exchange.call_args.args[1], response)


if __name__ == "__main__":
    unittest.main()
