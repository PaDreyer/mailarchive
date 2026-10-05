"""Failures exposed by persistence and execution application boundaries."""


class WorkspaceError(RuntimeError):
    """The selected local profile is unavailable or inconsistent."""


class RunNotActiveError(WorkspaceError):
    """Work may not advance after its run has stopped."""


class AuthorizationError(RuntimeError):
    """Provider sign-in or access-token acquisition failed."""


class AuthorizationRequiredError(AuthorizationError):
    """Provider access requires a new interactive authorization."""
