"""Public rule editing retains complete predicates and supports deliberate changes."""

import gc
import tkinter.font as tkfont
import unittest
import weakref
from copy import deepcopy
from unittest.mock import patch

from mailarchive.domain.configuration import (
    Condition,
    MailField,
    MatchMode,
    MatchOperator,
    Rule,
    RuleTarget,
)
from mailarchive.domain.mail_parser import parse_mail
from mailarchive.domain.rules import rule_matches
from mailarchive.presentation.dialogs import RuleDialog
from tests import test_desktop_composition as desktop_fixture
from tests.tk_test_case import TkTestCase


class RuleMatchingRoundtripTests(TkTestCase):
    def setUp(self):
        try:
            self.fixture = desktop_fixture.DesktopCompositionTests()
            self.fixture.setUp()
        except Exception:
            self.fixture.doCleanups()
            raise
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.app = self.fixture.application
        self.desktop = self.fixture.desktop

    def saved_rule(self, conditions, mode):
        rule = Rule(
            "Original",
            conditions=deepcopy(conditions),
            match_mode=mode,
            targets=[RuleTarget(str(self.app.database_path.parent / "archive"))],
        )
        self.app.save_rules([rule])
        self.desktop.settings = self.app.settings
        self.desktop.refresh_all()
        self.desktop.rule_tree.selection_set(rule.id)
        return rule

    def edit(self, action):
        errors = []

        def factory(*args, **kwargs):
            dialog = RuleDialog(*args, **kwargs)

            def run():
                try:
                    action(dialog)
                except Exception as exc:
                    errors.append(exc)
                    if dialog.winfo_exists():
                        dialog.destroy()

            self.root.after_idle(run)
            return dialog

        with patch("mailarchive.presentation.desktop.RuleDialog", side_effect=factory):
            with self.tk_timeout(lambda: self.root.destroy()):
                self.desktop.edit_rule()
        if errors:
            raise errors[0]

    def persisted(self):
        return self.app._context.execution.service.configuration.load_settings().rules[0]

    def test_metadata_only_edit_preserves_all_valid_predicates_and_matching(self):
        variants = (
            (
                [
                    Condition(MailField.SUBJECT, MatchOperator.CONTAINS, "Invoice"),
                    Condition(MailField.SENDER, MatchOperator.EQUALS, "trusted@example.org"),
                ],
                MatchMode.ALL,
            ),
            (
                [
                    Condition(MailField.SUBJECT, MatchOperator.CONTAINS, "Invoice"),
                    Condition(MailField.SUBJECT, MatchOperator.CONTAINS, "Receipt"),
                ],
                MatchMode.ANY,
            ),
            (
                [
                    Condition(MailField.SENDER, MatchOperator.CONTAINS, "example.org"),
                    Condition(MailField.SENDER, MatchOperator.CONTAINS, "trusted"),
                ],
                MatchMode.ALL,
            ),
            (
                [
                    Condition(MailField.SENDER, MatchOperator.CONTAINS, "trusted"),
                    Condition(MailField.SENDER, MatchOperator.EQUALS, "billing@example.org"),
                ],
                MatchMode.ANY,
            ),
            ([Condition(MailField.HAS_ATTACHMENT, value="")], MatchMode.ANY),
            ([Condition(MailField.SUBJECT, value="")], MatchMode.ALL),
            ([], MatchMode.ANY),
        )
        samples = [
            parse_mail(raw)
            for raw in (
                b"From: attacker@example.org\r\nSubject: Invoice\r\n\r\nBody",
                b"From: trusted@example.org\r\nSubject: Receipt\r\n\r\nBody",
                b"From: billing@example.org\r\nSubject: Invoice\r\n\r\nBody",
            )
        ]
        for conditions, mode in variants:
            with self.subTest(conditions=conditions, mode=mode):
                original = self.saved_rule(conditions, mode)

                def rename(dialog):
                    dialog.name_var.set("Renamed")
                    dialog.destinations.blocks[0].path_var.set(
                        str(self.app.database_path.parent / "new-target")
                    )
                    dialog.save_button.invoke()

                self.edit(rename)
                updated = self.persisted()
                self.assertEqual((updated.conditions, updated.match_mode), (conditions, mode))
                self.assertEqual(updated.id, original.id)
                self.assertEqual(updated.targets[0].id, original.targets[0].id)
                self.assertEqual(updated.name, "Renamed")
                self.assertNotEqual(updated.targets[0].path, original.targets[0].path)
                self.assertEqual(
                    [rule_matches(updated, mail) for mail in samples],
                    [rule_matches(original, mail) for mail in samples],
                )

    def test_editing_second_condition_and_match_mode_is_deliberate_and_persisted(self):
        self.saved_rule(
            [
                Condition(MailField.SUBJECT, value="Invoice"),
                Condition(MailField.SENDER, value="trusted"),
            ],
            MatchMode.ALL,
        )

        def change(dialog):
            editor = dialog.condition_editor
            self.assertEqual(len(editor.rows), 2)
            editor.rows[1].value_var.set("billing@example.org")
            editor.mode_var.set(MatchMode.ANY.value)
            dialog.save_button.invoke()

        self.edit(change)
        rule = self.persisted()
        self.assertEqual(rule.match_mode, MatchMode.ANY)
        self.assertEqual(rule.conditions[1].value, "billing@example.org")
        self.assertEqual(rule.conditions[0].value, "Invoice")

    def test_add_from_simple_form_requires_new_value_and_remove_keeps_other_rows(self):
        self.saved_rule([Condition(MailField.SUBJECT, value="Invoice")], MatchMode.ALL)

        def add(dialog):
            dialog.more_conditions_button.invoke()
            editor = dialog.condition_editor
            self.assertEqual(len(editor.rows), 2)
            with patch("mailarchive.presentation.dialogs.messagebox.showerror") as error:
                dialog.save_button.invoke()
            self.assertTrue(dialog.winfo_exists())
            error.assert_called_once()
            editor.rows[1].field_var.set("Sender")
            editor.rows[1]._update_fields()
            editor.rows[1].value_var.set("trusted@example.org")
            editor.add_button.invoke()
            self.assertEqual(len(editor.rows), 3)
            editor.rows[2].remove_button.invoke()
            dialog.save_button.invoke()

        self.edit(add)
        self.assertEqual(
            self.persisted().conditions,
            [
                Condition(MailField.SUBJECT, value="Invoice"),
                Condition(MailField.SENDER, value="trusted@example.org"),
            ],
        )

    def test_save_failure_preserves_complex_draft_and_cancel_leaves_profile_unchanged(self):
        original = self.saved_rule(
            [
                Condition(MailField.SUBJECT, value="Invoice"),
                Condition(MailField.BODY, value="Paid"),
            ],
            MatchMode.ANY,
        )

        def fail(dialog):
            dialog.condition_editor.rows[1].value_var.set("Outstanding")
            with patch.object(self.app._context, "save", side_effect=OSError("Disk full")):
                with patch("mailarchive.presentation.dialogs.messagebox.showerror") as error:
                    dialog.save_button.invoke()
            error.assert_called_once()
            self.assertTrue(dialog.winfo_exists())
            self.assertEqual(dialog.condition_editor.rows[1].value_var.get(), "Outstanding")
            self.assertEqual(self.persisted(), original)
            dialog.cancel_button.invoke()

        self.edit(fail)
        self.assertEqual(self.persisted(), original)

    def test_removed_condition_releases_widgets_and_callbacks(self):
        self.saved_rule(
            [
                Condition(MailField.SUBJECT, value="Invoice"),
                Condition(MailField.BODY, value="Paid"),
            ],
            MatchMode.ALL,
        )

        def remove(dialog):
            row = dialog.condition_editor.rows[1]
            reference = weakref.ref(row)
            commands = tuple(row.field_box._tclCommands)
            row.remove_button.invoke()
            del row
            gc.collect()
            self.assertIsNone(reference())
            for command in commands:
                self.assertFalse(self.root.tk.call("info", "commands", command))
            dialog.cancel_button.invoke()

        self.edit(remove)

    def test_dense_complex_rule_at_high_dpi_keeps_nested_inputs_and_footer_reachable(self):
        scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        self.root.tk.call("tk", "scaling", 192 / 72)
        # Tk caches fonts created by the desktop before the scaling change.
        for name in tkfont.names(self.root):
            font = tkfont.nametofont(name, self.root)
            font.configure(size=font.cget("size"))
        self.saved_rule(
            [
                Condition(MailField.SUBJECT if index % 2 else MailField.BODY, value=f"Term {index}")
                for index in range(20)
            ],
            MatchMode.ALL,
        )

        def check(dialog):
            self.assertLessEqual(dialog.winfo_width(), self.root.winfo_screenwidth() - 48)
            self.assertLessEqual(dialog.winfo_height(), self.root.winfo_screenheight() - 80)
            entry = dialog.condition_editor.rows[-1].value_entry
            dialog.focus_force()
            entry.focus_set()
            self.root.update()
            for viewport in (dialog.condition_editor.scroll.canvas, dialog.form_scroll.canvas):
                self.assertGreaterEqual(entry.winfo_rootx(), viewport.winfo_rootx())
                self.assertLessEqual(
                    entry.winfo_rootx() + entry.winfo_width(),
                    viewport.winfo_rootx() + viewport.winfo_width(),
                )
                self.assertGreaterEqual(entry.winfo_rooty(), viewport.winfo_rooty())
                self.assertLessEqual(
                    entry.winfo_rooty() + entry.winfo_height(),
                    viewport.winfo_rooty() + viewport.winfo_height(),
                )
            for button in (dialog.save_button, dialog.cancel_button):
                self.assertLessEqual(
                    button.winfo_rooty() + button.winfo_height(),
                    dialog.winfo_rooty() + dialog.winfo_height(),
                )
            dialog.save_button.invoke()

        self.edit(check)
        self.assertEqual(len(self.persisted().conditions), 20)


if __name__ == "__main__":
    unittest.main()
