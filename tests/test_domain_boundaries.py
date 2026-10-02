"""Enforce the dependency direction of the modular application."""

import ast
import unittest
from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "mailarchive"


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


class DomainBoundaryTests(unittest.TestCase):
    def test_modules_obey_dependency_direction(self) -> None:
        allowed = {
            "domain": {"domain"},
            "application": {"domain", "application"},
            "infrastructure": {"domain", "application", "infrastructure"},
            "presentation": {"domain", "application", "presentation"},
        }
        for layer, dependencies in allowed.items():
            for path in (PACKAGE_DIRECTORY / layer).rglob("*.py"):
                for module in imports(path):
                    if module.startswith("mailarchive."):
                        with self.subTest(
                            file=str(path.relative_to(PACKAGE_DIRECTORY)), module=module
                        ):
                            self.assertIn(module.split(".")[1], dependencies)

    def test_domain_and_application_do_not_import_sql_or_ui_toolkits(self) -> None:
        forbidden = {"sqlite3", "tkinter", "requests", "imaplib", "urllib", "PIL"}
        for layer in ("domain", "application"):
            for path in (PACKAGE_DIRECTORY / layer).rglob("*.py"):
                for module in imports(path):
                    with self.subTest(file=str(path.relative_to(PACKAGE_DIRECTORY)), module=module):
                        self.assertNotIn(module.split(".")[0], forbidden)


if __name__ == "__main__":
    unittest.main()
