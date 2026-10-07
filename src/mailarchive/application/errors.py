"""Failures exposed by persistence and execution application boundaries."""


class WorkspaceError(RuntimeError):
    """The selected local profile is unavailable or inconsistent."""


class ProfileUnavailableError(WorkspaceError):
    """A temporary profile-access failure can be retried without replacing its data."""


class RunNotActiveError(WorkspaceError):
    """Work may not advance after its run has stopped."""


class AuthorizationError(RuntimeError):
    """Provider sign-in or access-token acquisition failed."""


class AuthorizationRequiredError(AuthorizationError):
    """Provider access requires a new interactive authorization."""


class ExecutionShutdownError(RuntimeError):
    """Shutdown failures are separate from whether owned execution has stopped."""

    def __init__(self, *, stopped: bool, failures: tuple[str, ...]) -> None:
        self.stopped = stopped
        self.failures = failures
        super().__init__("; ".join(failures))


class ShutdownCleanupError(RuntimeError):
    """Unexpected owner failures after every shutdown owner was attempted."""

    def __init__(self, failures: tuple[Exception, ...]) -> None:
        self.failures = failures
        super().__init__("Could not finish MailArchive shutdown: " + "; ".join(map(str, failures)))
