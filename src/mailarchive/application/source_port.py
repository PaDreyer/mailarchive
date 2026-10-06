"""Provider-neutral read-only mail source contract."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import Account, Mailbox, MailHeaders
from mailarchive.domain.source_identity import MailTarget, MessageScope


class MailboxError(RuntimeError):
    @property
    def scan_wide(self) -> bool:
        return False


class RemoteMessageUnavailable(MailboxError):
    pass


class RemoteMessageError(MailboxError):
    """One stable provider message is malformed; later IDs may still be processed."""


class ScanWideProviderError(MailboxError):
    """A lazy stream failure must stop discovery for the current source scan."""

    @property
    def scan_wide(self) -> bool:
        return True


def is_scan_wide_error(error: BaseException) -> bool:
    """Classify source failures without exposing provider protocols to orchestration."""
    return isinstance(error, MailboxError) and error.scan_wide


@dataclass(frozen=True, slots=True)
class RemoteAccess(Cancellation):
    """Check account permission at provider checkpoints, independently of local work."""

    require_access: Callable[[], None] = lambda: None

    def checkpoint(self) -> None:
        Cancellation.checkpoint(self)
        self.require_access()


@dataclass(slots=True)
class RemoteMessage:
    id: str
    raw: bytes | None = None
    received_at: datetime | None = None
    received_origin: str = ""
    raw_chunks: Callable[[], Iterator[bytes]] | None = None
    raw_size: int | None = None
    release: Callable[[], None] | None = None
    error: Exception | None = None
    headers: MailHeaders | None = None

    def iter_raw(self) -> Iterator[bytes]:
        if self.raw is not None:
            yield self.raw
            return
        if self.raw_chunks is None:
            raise MailboxError(f"Message {self.id} did not contain MIME data.")
        yield from self.raw_chunks()

    def release_resources(self) -> None:
        if self.release is not None:
            release, self.release = self.release, None
            release()


MessageFilter = Callable[[MessageScope, str], bool]


class MessageSource(Protocol):
    def targets(
        self, account: Account, mailbox: Mailbox, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> list[MailTarget]: ...

    def fetch_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        *,
        sync: SyncSession | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]: ...

    def search_messages(
        self,
        target: MailTarget,
        should_fetch: MessageFilter,
        start: datetime | None,
        end: datetime | None,
        *,
        range_sync: RangePagination | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[MessageScope, Iterator[RemoteMessage]]: ...

    def fetch_message(
        self,
        target: MailTarget,
        remote_id: str,
        processing_namespace: str,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> RemoteMessage | None: ...


class SourceRegistry(Protocol):
    def get(self, account: Account) -> MessageSource: ...
