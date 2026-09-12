"""Read public stable releases without downloading or executing application code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mailarchive import __version__

REPOSITORY_URL = "https://github.com/PaDreyer/mailarchive"
LATEST_RELEASE_API = "https://api.github.com/repos/PaDreyer/mailarchive/releases/latest"
_STABLE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_MAX_RESPONSE_BYTES = 1024 * 1024


class UpdateError(RuntimeError):
    """An update check failed; normal archiving can continue."""


def _version_tuple(value: str) -> tuple[int, ...]:
    match = _STABLE_VERSION.fullmatch(value)
    if not match:
        raise ValueError(f"Unsupported release version: {value!r}")
    return tuple(int(part) for part in match.groups())


@dataclass(frozen=True, slots=True)
class Release:
    version: str

    @property
    def url(self) -> str:
        return f"{REPOSITORY_URL}/releases/tag/v{self.version}"


def check_for_update(current_version: str = __version__) -> Release | None:
    """Return a newer stable release, or None when none is published."""
    request = Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"MailArchive/{__version__}",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("The release response is too large.")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Invalid release response.")
        if value.get("draft") or value.get("prerelease"):
            return None
        tag = value.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError("The release does not contain a valid version tag.")
        version = tag[1:]
        if _version_tuple(version) > _version_tuple(current_version):
            return Release(version)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(
            f"GitHub update check failed (HTTP {exc.code}). Try again later."
        ) from exc
    except (URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Could not check for updates: {exc}") from exc
    return None
