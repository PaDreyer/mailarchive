"""Cooperative cancellation passed explicitly across processing boundaries."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass


class ProcessingStopped(Exception):
    """The caller stopped execution; this is not a provider or archive failure."""


@dataclass(frozen=True, slots=True)
class Cancellation:
    requested: Callable[[], bool] = lambda: False
    reason: str | Callable[[], str] = "Mail check stopped by the user."

    @property
    def message(self) -> str:
        return self.reason() if callable(self.reason) else self.reason

    def checkpoint(self) -> None:
        if self.requested():
            raise ProcessingStopped(self.message)

    def chunks(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        iterator = iter(chunks)
        try:
            while True:
                self.checkpoint()
                try:
                    chunk = next(iterator)
                except StopIteration:
                    return
                self.checkpoint()
                yield chunk
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()


NO_CANCELLATION = Cancellation()
