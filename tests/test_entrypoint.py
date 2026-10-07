import runpy
import unittest
from unittest import mock

from mailarchive import app


class EntrypointTests(unittest.TestCase):
    def test_module_entrypoint_starts_application(self) -> None:
        with mock.patch("mailarchive.app.main") as main:
            runpy.run_module("mailarchive.__main__", run_name="__main__")

        main.assert_called_once_with()

    def test_second_launch_activates_the_owner_and_closes_its_own_handle(self):
        with (
            mock.patch.object(app, "_parse_arguments", return_value=mock.Mock(smoke_test=False)),
            mock.patch.object(app, "SingleInstance") as constructor,
            mock.patch.object(app, "create_root") as create_root,
        ):
            instance = constructor.return_value
            instance.already_running = True
            app.main()
        instance.activate.assert_called_once_with()
        instance.close.assert_called_once_with()
        create_root.assert_not_called()

    def test_root_creation_failure_releases_the_instance_and_listener(self):
        with (
            mock.patch.object(app, "_parse_arguments", return_value=mock.Mock(smoke_test=False)),
            mock.patch.object(app, "SingleInstance") as constructor,
            mock.patch.object(app, "create_root", side_effect=RuntimeError("Tk failed")),
        ):
            constructor.return_value.already_running = False
            with self.assertRaisesRegex(RuntimeError, "Tk failed"):
                app.main()
        constructor.return_value.close.assert_called_once_with()

    def test_failed_secondary_activation_still_closes_its_handle(self):
        with (
            mock.patch.object(app, "_parse_arguments", return_value=mock.Mock(smoke_test=False)),
            mock.patch.object(app, "SingleInstance") as constructor,
        ):
            constructor.return_value.already_running = True
            constructor.return_value.activate.side_effect = OSError("Owner unavailable")
            with self.assertRaisesRegex(OSError, "Owner unavailable"):
                app.main()
        constructor.return_value.close.assert_called_once_with()

    def test_application_start_failure_releases_the_instance_and_listener(self):
        with (
            mock.patch.object(app, "_parse_arguments", return_value=mock.Mock(smoke_test=False)),
            mock.patch.object(app, "SingleInstance") as constructor,
            mock.patch.object(app, "create_root") as create_root,
            mock.patch.object(app, "ConfigStore"),
            mock.patch.object(app, "KeyringCredentialStore"),
            mock.patch.object(app, "WindowsCredentialStore"),
            mock.patch.object(
                app, "create_application", side_effect=RuntimeError("Profile failed")
            ),
            mock.patch.object(app.messagebox, "showerror"),
        ):
            constructor.return_value.already_running = False
            app.main()
        constructor.return_value.set_activation_handler.assert_not_called()
        constructor.return_value.close.assert_called_once_with()
        create_root.return_value.destroy.assert_called_once_with()

    def test_activation_handler_is_attached_after_the_minimized_start_decision(self):
        for minimized in (False, True):
            with (
                self.subTest(minimized=minimized),
                mock.patch.object(
                    app,
                    "_parse_arguments",
                    return_value=mock.Mock(smoke_test=False, minimized=minimized),
                ),
                mock.patch.object(app, "SingleInstance") as constructor,
                mock.patch.object(app, "create_root") as create_root,
                mock.patch.object(app, "ConfigStore"),
                mock.patch.object(app, "KeyringCredentialStore"),
                mock.patch.object(app, "WindowsCredentialStore"),
                mock.patch.object(app, "create_application"),
                mock.patch.object(app, "DesktopApp") as desktop,
                mock.patch.object(app.AppImageIntegration, "for_current_process"),
            ):
                constructor.return_value.already_running = False
                root = create_root.return_value
                attached = []

                def attach(callback, attached=attached, root=root, minimized=minimized):
                    attached.append(True)
                    self.assertEqual(root.withdraw.called, minimized)
                    callback()

                constructor.return_value.set_activation_handler.side_effect = attach
                root.mainloop.side_effect = lambda attached=attached: self.assertTrue(attached)
                app.main()
                desktop.return_value.post_ui.assert_called_once_with(desktop.return_value.show)
                constructor.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
