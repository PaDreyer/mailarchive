from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from mailarchive import __version__
from mailarchive.desktop_entry import autostart_entry, desktop_exec, desktop_value
from mailarchive.linux_integration import (
    AppImageIntegration,
    IntegrationError,
    IntegrationOptions,
    IntegrationPaths,
    IntegrationState,
    _FileTransaction,
    managed_appimage,
)

APPIMAGE = b"\x7fELF\x02\x01\x01\x00AI\x02" + b"application contents"


class DesktopEntryTests(unittest.TestCase):
    def test_exec_uses_both_escape_layers_and_literal_field_codes(self) -> None:
        self.assertEqual(
            desktop_exec(['/tmp/a "$x`y\\%f.AppImage', "--minimized"]),
            r'"/tmp/a \\"\\$x\\`y\\\\%%f.AppImage" "--minimized"',
        )
        self.assertEqual(desktop_exec(["/tmp/Grüße AppImage"]), '"/tmp/Grüße AppImage"')

    def test_string_controls_do_not_inject_desktop_keys(self) -> None:
        self.assertEqual(desktop_value("a\nb\rc\td\\e"), r"a\nb\rc\td\\e")
        self.assertIn(r"\nExec=bad", desktop_exec(["/tmp/app", "/tmp/test\nExec=bad"]))

    def test_unsupported_exec_paths_and_nul_are_rejected(self) -> None:
        for command in ([], ["/tmp/a=b"], ["/tmp/a\0b"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                desktop_exec(command)

    def test_windows_style_icon_paths_are_escaped_as_desktop_values(self) -> None:
        path = PureWindowsPath(r"C:\Users\Example\MailArchive\mailarchive.svg")
        self.assertEqual(
            desktop_value(str(path)), r"C:\\Users\\Example\\MailArchive\\mailarchive.svg"
        )

    def test_launcher_serializes_windows_paths_independently_of_host_os(self) -> None:
        home = PureWindowsPath("C:/Users/Example")
        paths = IntegrationPaths(home / "data", home / "config", home)
        integration = AppImageIntegration(home / "download.AppImage", None, paths)
        launcher = integration._launcher().decode()
        self.assertIn(
            r"Icon=C:\\Users\\Example\\data\\mailarchive\\application\\mailarchive.svg" + "\n",
            launcher,
        )
        self.assertIn(
            r'Exec="C:\\\\Users\\\\Example\\\\data\\\\mailarchive\\\\application\\\\MailArchive.AppImage"'
            + "\n",
            launcher,
        )


class LinuxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.desktop = self.home / "Schreibtisch"
        self.desktop.mkdir(parents=True)
        self.paths = IntegrationPaths(self.home / "data", self.home / "config", self.home)
        self.paths.config_home.mkdir()
        self._set_desktop("$HOME/Schreibtisch")
        self.source = self.root / "Downloads" / "Mail Archive.AppImage"
        self.source.parent.mkdir()
        self.source.write_bytes(APPIMAGE)
        self.icon_source = self.root / "AppDir" / "mailarchive.svg"
        self.icon_source.parent.mkdir()
        self.icon_source.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        self.integration = AppImageIntegration(self.source, self.icon_source, self.paths)
        trust = patch.object(self.integration, "_trust_desktop", return_value=())
        trust.start()
        self.addCleanup(trust.stop)

    def _set_desktop(self, value: str) -> None:
        (self.paths.config_home / "user-dirs.dirs").write_text(
            f'XDG_DESKTOP_DIR="{value}"\n', encoding="utf-8"
        )

    def _files(self) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def _install(self, *, autostart: bool = True) -> None:
        if autostart:
            self.paths.autostart.parent.mkdir(parents=True, exist_ok=True)
            self.paths.autostart.write_text(
                autostart_entry([str(self.source), "--minimized"]), encoding="utf-8"
            )
        self.integration.apply(IntegrationOptions(True, True), start_at_login=autostart)

    def test_install_copies_app_and_icon_and_uses_stable_paths_everywhere(self) -> None:
        self._install()
        self.assertEqual(self.source.read_bytes(), APPIMAGE)
        self.assertEqual(self.paths.application.read_bytes(), APPIMAGE)
        self.assertEqual(self.paths.icon.read_bytes(), self.icon_source.read_bytes())
        menu = self.paths.menu.read_text(encoding="utf-8")
        self.assertIn(f"Exec={desktop_exec([str(self.paths.application)])}\n", menu)
        self.assertIn(f"Icon={desktop_value(str(self.paths.icon))}\n", menu)
        self.assertIn("StartupWMClass=Mailarchive\n", menu)
        self.assertNotIn(str(self.source), menu)
        self.assertEqual(menu, (self.desktop / "MailArchive.desktop").read_text(encoding="utf-8"))
        self.assertIn(
            desktop_exec([str(self.paths.application), "--minimized"]),
            self.paths.autostart.read_text(encoding="utf-8"),
        )
        state = self.integration.load_state()
        self.assertTrue(state.prompt_seen)
        self.assertEqual(state.installed_version, __version__)
        self.assertEqual(state.options, IntegrationOptions(True, True))
        if os.name != "nt":
            self.assertEqual(self.paths.application.stat().st_mode & 0o777, 0o755)
            self.assertEqual((self.desktop / "MailArchive.desktop").stat().st_mode & 0o777, 0o755)
            self.assertEqual(self.paths.receipt.stat().st_mode & 0o777, 0o600)

    def test_menu_only_and_desktop_only_are_independent(self) -> None:
        self.integration.apply(IntegrationOptions(True, False), start_at_login=False)
        self.assertTrue(self.paths.menu.exists())
        self.assertFalse((self.desktop / "MailArchive.desktop").exists())
        self.integration.apply(IntegrationOptions(False, True), start_at_login=False)
        self.assertFalse(self.paths.menu.exists())
        self.assertTrue((self.desktop / "MailArchive.desktop").exists())
        self.assertFalse(self.paths.autostart.exists())

    def test_skip_creates_only_receipt_and_preserves_settings(self) -> None:
        settings = self.paths.receipt.parent / "config.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("user settings", encoding="utf-8")
        self.integration.mark_prompt_seen()
        self.assertEqual(settings.read_text(encoding="utf-8"), "user settings")
        self.assertEqual(self.integration.load_state(), IntegrationState(prompt_seen=True))
        self.assertFalse(self.paths.application.exists())
        self.assertFalse(self.paths.menu.exists())

    def test_clearing_shortcuts_keeps_installation_and_autostart(self) -> None:
        self._install()
        application = self.paths.application.read_bytes()
        autostart = self.paths.autostart.read_bytes()
        self.source.unlink()
        self.integration.apply(IntegrationOptions(False, False), start_at_login=True)
        self.assertFalse(self.paths.menu.exists())
        self.assertFalse((self.desktop / "MailArchive.desktop").exists())
        self.assertEqual(self.paths.application.read_bytes(), application)
        self.assertEqual(self.paths.autostart.read_bytes(), autostart)
        self.assertEqual(self.integration.load_state().installed_version, __version__)

    def test_reconfiguration_still_works_after_deleting_the_download(self) -> None:
        self._install(autostart=False)
        self.source.unlink()
        self.icon_source.unlink()
        self.integration.apply(IntegrationOptions(True, False), start_at_login=False)
        self.assertEqual(self.paths.application.read_bytes(), APPIMAGE)
        self.assertTrue(self.paths.icon.exists())
        self.assertTrue(self.paths.menu.exists())
        self.assertFalse((self.desktop / "MailArchive.desktop").exists())

    def test_missing_installation_never_retargets_autostart_to_a_missing_file(self) -> None:
        self._install()
        self.paths.application.unlink()
        self.paths.autostart.write_text(
            autostart_entry([str(self.source), "--minimized"]), encoding="utf-8"
        )
        before = self.paths.autostart.read_bytes()
        self.integration.apply(IntegrationOptions(False, False), start_at_login=True)
        self.assertEqual(self.paths.autostart.read_bytes(), before)

    def test_shortcut_removal_failure_restores_the_previous_shortcuts(self) -> None:
        self._install()
        before = self._files()
        real_replace = os.replace

        def fail_receipt(source, target):
            if target == self.paths.receipt and "backup" not in Path(source).name:
                raise OSError("cannot save receipt")
            return real_replace(source, target)

        with patch("mailarchive.linux_integration.os.replace", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.integration.apply(IntegrationOptions(False, False), start_at_login=True)
        self.assertEqual(self._files(), before)

    def test_apply_without_any_shortcuts_does_not_install(self) -> None:
        self.integration.apply(IntegrationOptions(False, False), start_at_login=True)
        self.assertEqual(self.integration.load_state(), IntegrationState(prompt_seen=True))
        self.assertFalse(self.paths.application.exists())
        self.assertFalse(self.paths.autostart.exists())

    def test_updates_replace_application_without_truncating_running_inode(self) -> None:
        self._install()
        if os.name == "nt":
            original = None  # Windows prevents replacing an open file; AppImages run on Linux.
        else:
            original = self.paths.application.open("rb")
            self.addCleanup(original.close)
        self.source.write_bytes(APPIMAGE + b"new version")
        with patch("mailarchive.linux_integration.__version__", "1.1.0"):
            self.integration.apply(IntegrationOptions(True, True), start_at_login=True)
        if original:
            self.assertEqual(original.read(), APPIMAGE)
        self.assertEqual(self.paths.application.read_bytes(), APPIMAGE + b"new version")
        self.assertEqual(self.integration.load_state().installed_version, "1.1.0")

    def test_running_installed_copy_can_reconfigure_without_copying_onto_itself(self) -> None:
        self._install(autostart=False)
        integration = AppImageIntegration(self.paths.application, self.icon_source, self.paths)
        with patch.object(integration, "_trust_desktop", return_value=()):
            integration.apply(IntegrationOptions(True, False), start_at_login=False)
        self.assertEqual(self.paths.application.read_bytes(), APPIMAGE)

    def test_moved_desktop_folder_replaces_old_managed_shortcut(self) -> None:
        self._install(autostart=False)
        other = self.home / "Bureau"
        other.mkdir()
        self._set_desktop("${HOME}/Bureau")
        self.integration.apply(IntegrationOptions(True, True), start_at_login=False)
        self.assertFalse((self.desktop / "MailArchive.desktop").exists())
        self.assertTrue((other / "MailArchive.desktop").exists())

    def test_disabled_missing_and_relative_desktops_are_not_created(self) -> None:
        for value in ("$HOME", "$HOME/.", "$HOME/Missing", "relative", "$(touch /tmp/not-run)"):
            with self.subTest(value=value):
                self._set_desktop(value)
                self.assertIsNone(self.paths.desktop_directory())
                with self.assertRaises(IntegrationError):
                    self.integration.apply(IntegrationOptions(True, True), start_at_login=False)
        self.assertFalse(self.paths.application.exists())
        self.integration.apply(IntegrationOptions(True, False), start_at_login=False)
        self.assertTrue(self.paths.menu.exists())

    def test_default_desktop_is_used_only_if_it_already_exists(self) -> None:
        (self.paths.config_home / "user-dirs.dirs").unlink()
        self.assertIsNone(self.paths.desktop_directory())
        (self.home / "Desktop").mkdir()
        self.assertEqual(self.paths.desktop_directory(), self.home / "Desktop")

    def test_malformed_user_directory_values_are_rejected(self) -> None:
        for value in ('"unterminated', '"/tmp/one" "/tmp/two"'):
            with self.subTest(value=value):
                (self.paths.config_home / "user-dirs.dirs").write_text(
                    f"XDG_DESKTOP_DIR={value}\n", encoding="utf-8"
                )
                self.assertIsNone(self.paths.desktop_directory())

    def test_existing_foreign_menu_desktop_and_autostart_are_preserved(self) -> None:
        for path in (self.paths.menu, self.desktop / "MailArchive.desktop", self.paths.autostart):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("another application's file", encoding="utf-8")
                before = self._files()
                with self.assertRaises(IntegrationError):
                    self.integration.apply(IntegrationOptions(True, True), start_at_login=True)
                self.assertEqual(self._files(), before)
                path.unlink()

    def test_unregistered_binary_and_icon_are_never_overwritten(self) -> None:
        for path in (self.paths.application, self.paths.icon):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"unregistered file")
                before = self._files()
                with self.assertRaises(IntegrationError):
                    self.integration.apply(IntegrationOptions(), start_at_login=False)
                self.assertEqual(self._files(), before)
                path.unlink()

    @unittest.skipIf(os.name == "nt", "Unix symlink handling")
    def test_symlink_targets_are_never_followed_or_overwritten(self) -> None:
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        for path in (self.paths.menu, self.paths.receipt, self.paths.application, self.paths.icon):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(victim)
                with self.assertRaises(IntegrationError):
                    self.integration.apply(IntegrationOptions(), start_at_login=False)
                self.assertTrue(path.is_symlink())
                self.assertEqual(victim.read_bytes(), b"keep")
                path.unlink()

    def test_invalid_sources_fail_before_installing_anything(self) -> None:
        for content in (b"not an AppImage", b"\x7fELFwithout AppImage magic"):
            self.source.write_bytes(content)
            with self.assertRaises(IntegrationError):
                self.integration.apply(IntegrationOptions(), start_at_login=False)
            self.assertFalse(self.paths.application.exists())
        self.source.write_bytes(APPIMAGE)
        for source, icon in ((Path("relative.AppImage"), self.icon_source), (self.source, None)):
            with self.subTest(source=source, icon=icon), self.assertRaises(IntegrationError):
                AppImageIntegration(source, icon, self.paths).apply(
                    IntegrationOptions(), start_at_login=False
                )

    def test_corrupt_and_future_receipts_are_not_silently_overwritten(self) -> None:
        payloads = (
            "not JSON",
            "[]",
            '{"schema_version": 2}',
            '{"prompt_seen": "yes"}',
            '{"schema_version": true}',
            '{"installed_version": false}',
            '{"desktop_path": "/tmp/other.txt"}',
            '{"desktop_path": "MailArchive.desktop"}',
            '{"unknown": 1}',
        )
        self.paths.receipt.parent.mkdir(parents=True)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.paths.receipt.write_text(payload, encoding="utf-8")
                with self.assertRaises(IntegrationError):
                    self.integration.mark_prompt_seen()
                self.assertEqual(self.paths.receipt.read_text(encoding="utf-8"), payload)

    def test_disk_full_during_staging_leaves_existing_installation_untouched(self) -> None:
        self._install()
        before = self._files()
        with patch(
            "mailarchive.linux_integration.shutil.copyfileobj", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.integration.apply(IntegrationOptions(True, True), start_at_login=True)
        self.assertEqual(self._files(), before)

    def test_source_modified_during_copy_is_rejected(self) -> None:
        copy = shutil.copyfileobj

        def mutate(original, target):
            copy(original, target)
            self.source.write_bytes(APPIMAGE + b"changed during copy")

        with patch("mailarchive.linux_integration.shutil.copyfileobj", side_effect=mutate):
            with self.assertRaises(IntegrationError):
                self.integration.apply(IntegrationOptions(), start_at_login=False)
        self.assertFalse(self.paths.application.exists())

    def test_every_update_commit_failure_rolls_back_all_files_and_modes(self) -> None:
        self._install()
        self.source.write_bytes(APPIMAGE + b"updated")
        before = self._files()
        real_replace = os.replace
        for fail_at in range(1, 13):  # Six existing targets: backup + replacement for each.
            with self.subTest(fail_at=fail_at):
                count = 0

                def fail_once(source, target, fail_at=fail_at):
                    nonlocal count
                    count += 1
                    if count == fail_at:
                        raise OSError("injected commit failure")
                    return real_replace(source, target)

                with patch("mailarchive.linux_integration.os.replace", side_effect=fail_once):
                    with self.assertRaises(OSError):
                        self.integration.apply(IntegrationOptions(True, True), start_at_login=True)
                self.assertEqual(self._files(), before)

    def test_new_install_commit_failure_removes_partially_installed_files(self) -> None:
        before = self._files()
        real_replace = os.replace

        def fail_receipt(source, target):
            if target == self.paths.receipt:
                raise OSError("receipt write failed")
            return real_replace(source, target)

        with patch("mailarchive.linux_integration.os.replace", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.integration.apply(IntegrationOptions(True, True), start_at_login=False)
        self.assertEqual(self._files(), before)

    def test_file_changed_after_staging_is_not_overwritten(self) -> None:
        target = self.root / "target"
        target.write_bytes(b"old")
        with _FileTransaction() as transaction:
            transaction.write(target, b"new")
            target.write_bytes(b"someone else's change")
            with self.assertRaises(IntegrationError):
                transaction.commit()
        self.assertEqual(target.read_bytes(), b"someone else's change")

    def test_failed_rollback_preserves_backup_for_recovery(self) -> None:
        self._install()
        original = self.paths.application.read_bytes()
        real_replace = os.replace

        def fail(source, target):
            if target == self.paths.menu:
                raise OSError("cannot write menu")
            if target == self.paths.application and "backup" in Path(source).name:
                raise OSError("cannot restore application")
            return real_replace(source, target)

        with patch("mailarchive.linux_integration.os.replace", side_effect=fail):
            with self.assertRaisesRegex(IntegrationError, "backup"):
                self.integration.apply(IntegrationOptions(True, True), start_at_login=True)
        backups = list(self.paths.application.parent.glob(".mailarchive-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_managed_appimage_requires_receipt_and_executable_file(self) -> None:
        with patch(
            "mailarchive.linux_integration.IntegrationPaths.defaults", return_value=self.paths
        ):
            self.assertIsNone(managed_appimage())
            self._install(autostart=False)
            self.assertEqual(managed_appimage(), self.paths.application)
            self.paths.application.unlink()
            self.assertIsNone(managed_appimage())
            self.paths.receipt.write_text("broken", encoding="utf-8")
            self.assertIsNone(managed_appimage())

    def test_managed_appimage_unavailable_home_does_not_break_optional_lookup(self) -> None:
        with patch(
            "mailarchive.linux_integration.Path.home",
            side_effect=RuntimeError("Could not determine home directory."),
        ):
            self.assertIsNone(managed_appimage())

    def test_process_detection_only_accepts_linux_appimage_runs(self) -> None:
        for platform, appimage in (
            ("win32", str(self.source)),
            ("linux", ""),
            ("darwin", str(self.source)),
        ):
            with (
                self.subTest(platform=platform, appimage=appimage),
                patch("mailarchive.linux_integration.sys.platform", platform),
                patch.dict(os.environ, {"APPIMAGE": appimage}, clear=True),
            ):
                self.assertIsNone(AppImageIntegration.for_current_process())
        with (
            patch("mailarchive.linux_integration.sys.platform", "linux"),
            patch.dict(
                os.environ, {"APPIMAGE": str(self.source), "APPDIR": str(self.icon_source.parent)}
            ),
        ):
            integration = AppImageIntegration.for_current_process()
            self.assertEqual(integration.source, self.source)
            self.assertEqual(integration.icon_source, self.icon_source)

    def test_xdg_overrides_must_be_absolute(self) -> None:
        with (
            patch("mailarchive.linux_integration.Path.home", return_value=self.home),
            patch.dict(
                os.environ, {"XDG_DATA_HOME": "relative", "XDG_CONFIG_HOME": ""}, clear=True
            ),
        ):
            paths = IntegrationPaths.defaults()
        self.assertEqual(paths.data_home, self.home / ".local" / "share")
        self.assertEqual(paths.config_home, self.home / ".config")

    def test_desktop_trust_is_best_effort_and_never_undoes_installation(self) -> None:
        self.integration._trust_desktop = AppImageIntegration._trust_desktop.__get__(
            self.integration
        )
        with patch("mailarchive.linux_integration.subprocess.run", side_effect=FileNotFoundError):
            result = self.integration.apply(IntegrationOptions(True, True), start_at_login=False)
        self.assertTrue(self.paths.application.exists())
        self.assertIn("Allow Launching", result.warnings[0])
        with patch("mailarchive.linux_integration.subprocess.run") as run:
            result = self.integration.apply(IntegrationOptions(True, True), start_at_login=False)
        self.assertEqual(result.warnings, ())
        self.assertEqual(
            run.call_args.args[0],
            ["gio", "set", str(self.desktop / "MailArchive.desktop"), "metadata::trusted", "true"],
        )

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate unavailable")
    def test_generated_launchers_pass_freedesktop_validator(self) -> None:
        self._install()
        for path in (self.paths.menu, self.desktop / "MailArchive.desktop", self.paths.autostart):
            result = subprocess.run(
                ["desktop-file-validate", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
