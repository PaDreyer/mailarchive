"""Local activation is isolated from the real desktop and credential stores."""

import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.infrastructure.platform_integration import SingleInstance
from mailarchive.presentation.desktop import DesktopApp
from tests.concurrency import THREAD_TIMEOUT
from tests.tk_test_case import TkTestCase


@unittest.skipIf(os.name == "nt", "Unix activation sockets")
class InstanceActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.first = SingleInstance("MailArchive-ActivationTest")
        self.addCleanup(self.first.close)
        self.second = SingleInstance("MailArchive-ActivationTest")
        self.addCleanup(self.second.close)

    def test_another_process_activates_the_owner_before_and_after_ui_attachment(self):
        self.assertFalse(self.first.already_running)
        self.assertTrue(self.second.already_running)
        script = (
            "from mailarchive.infrastructure.platform_integration import SingleInstance; "
            "instance=SingleInstance('MailArchive-ActivationTest'); "
            "assert instance.already_running; assert instance.activate(); instance.close()"
        )
        subprocess.run([sys.executable, "-c", script], check=True, timeout=THREAD_TIMEOUT)
        delivered = queue.Queue()
        self.first.set_activation_handler(lambda: delivered.put(threading.get_ident()))
        self.assertIn(
            delivered.get(timeout=THREAD_TIMEOUT),
            (threading.get_ident(), self.first._activation_thread.ident),
        )
        for _ in range(3):
            self.assertTrue(self.second.activate())
            self.assertEqual(
                delivered.get(timeout=THREAD_TIMEOUT), self.first._activation_thread.ident
            )

    def test_closed_owner_discards_callbacks_and_releases_socket_thread_and_lock(self):
        delivered = queue.Queue()
        self.first.set_activation_handler(lambda: delivered.put(True))
        self.assertTrue(self.second.activate())
        self.assertTrue(delivered.get(timeout=THREAD_TIMEOUT))
        path = self.first._activation_path
        worker = self.first._activation_thread
        self.second.close()
        self.assertTrue(path.exists(), "A secondary must not remove the owner's socket")
        self.first.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(path.exists())
        self.first.set_activation_handler(lambda: self.fail("Closed instance received activation"))
        self.assertFalse(self.first.activate())
        replacement = SingleInstance("MailArchive-ActivationTest")
        try:
            self.assertFalse(replacement.already_running)
            self.assertTrue(replacement._activation_thread.is_alive())
        finally:
            replacement.close()
        self.assertFalse(path.exists())

    def test_stale_owned_socket_is_recovered_and_unrelated_files_are_preserved(self):
        self.second.close()
        self.first.close()
        path = self.first._activation_path
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as stale:
            stale.bind(str(path))
        replacement = SingleInstance("MailArchive-ActivationTest")
        replacement.close()
        self.assertFalse(path.exists())
        path.write_text("unrelated file", encoding="utf-8")
        with self.assertLogs("mailarchive.infrastructure.platform_integration", "WARNING"):
            replacement = SingleInstance("MailArchive-ActivationTest")
        try:
            self.assertFalse(replacement.already_running)
            self.assertIsNone(replacement._activation_thread)
            self.assertFalse(replacement.activate())
            self.assertEqual(path.read_text(encoding="utf-8"), "unrelated file")
        finally:
            replacement.close()

    def test_socket_symlink_and_unsafe_runtime_directory_are_rejected_without_mutation(self):
        self.second.close()
        self.first.close()
        target = self.runtime / "unrelated"
        target.write_text("keep", encoding="utf-8")
        path = self.first._activation_path
        path.symlink_to(target)
        with self.assertLogs("mailarchive.infrastructure.platform_integration", "WARNING"):
            replacement = SingleInstance("MailArchive-ActivationTest")
        replacement.close()
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        self.runtime.chmod(0o777)
        try:
            with self.assertRaisesRegex(RuntimeError, "Unsafe runtime directory"):
                SingleInstance("MailArchive-Other")
        finally:
            self.runtime.chmod(0o700)

    def test_unrecognized_messages_do_not_activate_and_socket_is_user_private(self):
        self.assertEqual(self.first._activation_path.stat().st_mode & 0o777, 0o600)
        delivered = queue.Queue()
        self.first.set_activation_handler(lambda: delivered.put(True))
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(b"wrong command", str(self.first._activation_path))
        self.assertTrue(self.second.activate())
        self.assertTrue(delivered.get(timeout=THREAD_TIMEOUT))
        self.first.close()
        self.assertTrue(delivered.empty())


@unittest.skipIf(os.name == "nt", "Unix activation sockets")
class InstanceActivationTkTests(TkTestCase):
    def test_early_and_later_activations_restore_the_real_window_on_the_tk_thread(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary.name}):
            first = SingleInstance("MailArchive-TkActivation")
            self.addCleanup(first.close)
            second = SingleInstance("MailArchive-TkActivation")
            self.addCleanup(second.close)
        desktop = object.__new__(DesktopApp)
        desktop.root = root
        desktop._closing = False
        desktop.ui_queue = queue.Queue()
        threads = []

        def show():
            threads.append(threading.get_ident())
            desktop.show()

        root.withdraw()
        root.update()
        self.assertTrue(second.activate())
        first.set_activation_handler(lambda: desktop.post_ui(show))
        callback = desktop.ui_queue.get(timeout=THREAD_TIMEOUT)
        self.assertEqual(root.state(), "withdrawn")
        callback()
        root.update()
        self.assertEqual(root.state(), "normal")
        self.assertEqual(threads, [threading.get_ident()])
        root.withdraw()
        self.assertTrue(second.activate())
        callback = desktop.ui_queue.get(timeout=THREAD_TIMEOUT)
        callback()
        root.update()
        self.assertEqual(root.state(), "normal")
        self.assertEqual(threads, [threading.get_ident()] * 2)
        root.withdraw()
        self.assertTrue(second.activate())
        callback = desktop.ui_queue.get(timeout=THREAD_TIMEOUT)
        desktop._closing = True
        callback()
        root.update()
        self.assertEqual(root.state(), "withdrawn")
        first.close()
        second.close()
