"""Shared freedesktop serialization for launchers and login autostart."""

from __future__ import annotations

from mailarchive import APP_WINDOW_CLASS

MANAGED_KEY = "X-MailArchive-Managed=true"


def is_managed_entry(content: str, *, legacy_autostart: bool = False) -> bool:
    lines = content.splitlines()
    if MANAGED_KEY in lines:
        return True
    # Recognize the exact format written by older MailArchive versions. Do not
    # adopt arbitrary user-created entries merely because they share our name.
    return legacy_autostart and (
        len(lines) == 7
        and lines[:4]
        == [
            "[Desktop Entry]",
            "Type=Application",
            "Name=MailArchive",
            "Comment=Automatically archive emails on this computer",
        ]
        and lines[4].startswith("Exec=")
        and lines[5:] == ["Terminal=false", "X-GNOME-Autostart-enabled=true"]
    )


def desktop_value(value: str) -> str:
    if "\0" in value:
        raise ValueError("Desktop entry values cannot contain NUL characters.")
    return (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )


def desktop_exec(arguments: list[str]) -> str:
    """Quote arguments at both the Exec and desktop-file string layers.

    This is not shell quoting. In particular, literal field-code prefixes and
    dollar signs must survive desktop launcher parsing unchanged.
    """
    if not arguments or "=" in arguments[0]:
        raise ValueError("Desktop launchers require an executable path without '='.")
    quoted = []
    for argument in arguments:
        escaped = argument.replace("%", "%%").replace("\\", "\\\\")
        for character in ('"', "`", "$"):
            escaped = escaped.replace(character, "\\" + character)
        quoted.append(desktop_value('"' + escaped + '"'))
    return " ".join(quoted)


def autostart_entry(arguments: list[str]) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=MailArchive\n"
        "Comment=Automatically archive emails on this computer\n"
        f"Exec={desktop_exec(arguments)}\n"
        "Terminal=false\n"
        f"StartupWMClass={APP_WINDOW_CLASS}\n"
        "X-GNOME-Autostart-enabled=true\n"
        f"{MANAGED_KEY}\n"
    )
