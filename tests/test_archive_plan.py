"""Deterministic planning before durable delivery and filesystem publication."""

import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mailarchive.domain.archive_plan import plan_outputs
from mailarchive.domain.configuration import Rule, RuleTarget, SaveMode
from tests.helpers import sample_mail


class ArchivePlanTests(unittest.TestCase):
    def test_mail_and_duplicate_attachments_are_distinct_per_target(self) -> None:
        raw = sample_mail(attachments=[("copy.txt", b"same"), ("copy.txt", b"same")])
        root = Path.cwd() / "archive-plan-targets"
        first = RuleTarget(str(root / "first"), id="first")
        second = RuleTarget(str(root / "second"), SaveMode.ATTACHMENTS_ONLY, id="second")
        rule = Rule("Copies", targets=[first, second])
        kwargs = dict(
            received_at=datetime(2026, 9, 11, 7, 30, tzinfo=timezone.utc),
            timezone_name="Europe/Berlin",
            source_id="source",
            message_key="message",
        )

        artifacts = plan_outputs(raw, rule, **kwargs)

        self.assertEqual(len(artifacts), 5)
        self.assertEqual([item.target_id for item in artifacts], ["first"] * 3 + ["second"] * 2)
        self.assertEqual(artifacts[0].content, raw)
        self.assertEqual(artifacts[0].digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(artifacts[0].requested_path.parent, root / "first")
        self.assertTrue(artifacts[0].requested_path.name.endswith(".eml"))
        self.assertNotEqual(artifacts[1].artifact_key, artifacts[2].artifact_key)
        self.assertNotEqual(artifacts[1].requested_path, artifacts[2].requested_path)
        self.assertEqual(artifacts[1].artifact_key, artifacts[3].artifact_key)
        self.assertEqual(plan_outputs(raw, rule, **kwargs), artifacts)


if __name__ == "__main__":
    unittest.main()
