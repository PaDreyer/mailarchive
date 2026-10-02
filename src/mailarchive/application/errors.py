"""Failures exposed by persistence and execution application boundaries."""


class WorkspaceError(RuntimeError):
    """The selected local profile is unavailable or inconsistent."""


class RunNotActiveError(WorkspaceError):
    """Work may not advance after its run has stopped."""
