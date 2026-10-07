"""Keep published archives separate from a profile's reserved storage."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Formatter
from uuid import uuid4

from mailarchive.domain.archive_paths import destination_path
from mailarchive.domain.configuration import Rule, Settings


@dataclass(frozen=True, slots=True)
class ArchiveDestinationPolicy:
    profile_directory: Path

    def require_path(self, directory: Path) -> None:
        directory = directory.resolve()
        profile = self.profile_directory.resolve()
        work = profile / "work"
        if directory == profile or directory == work or work in directory.parents:
            raise ValueError(
                "Choose an archive destination outside the reserved profile root and work directory."
            )

    def require_rule(self, rule: Rule, *, mail_date: datetime | None = None) -> None:
        for target in rule.targets:
            preview = destination_path(target.path, mail_date=mail_date)
            if mail_date is None:
                self._require_template(target.path)
            else:
                self.require_path(preview)

    def require_settings(self, settings: Settings) -> None:
        for rule in settings.rules:
            self.require_rule(rule)

    def _require_template(self, template: str) -> None:
        # Resolve literal parent aliases without confusing escaped braces or
        # actual numeric profile directories with a YYYY/MM preview.
        markers = {field: "mailarchive_" + uuid4().hex for field in ("year", "month")}
        symbolic = Path(
            "".join(
                literal + (markers[field] if field else "")
                for literal, field, _spec, _conversion in Formatter().parse(template)
            )
        ).resolve()

        def matches(candidate: Path, reserved: Path) -> bool:
            pattern = re.escape(os.path.normcase(str(candidate)))
            for field, marker in markers.items():
                escaped = re.escape(marker)
                if escaped in pattern:
                    values = r"(?!0000)[0-9]{4}" if field == "year" else r"(?:0[1-9]|1[0-2])"
                    pattern = pattern.replace(escaped, f"(?P<{field}>{values})", 1)
                    pattern = pattern.replace(escaped, f"(?P={field})")
            return re.fullmatch(pattern, os.path.normcase(str(reserved))) is not None

        profile = self.profile_directory.resolve()
        if matches(symbolic, profile) or any(
            matches(candidate, profile / "work") for candidate in (symbolic, *symbolic.parents)
        ):
            raise ValueError(
                "Choose an archive destination outside the reserved profile root and work directory."
            )
