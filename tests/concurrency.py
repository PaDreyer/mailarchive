"""Observe actual thread synchronization; timeouts only bound broken tests."""

THREAD_TIMEOUT = 5


class ObservedLock:
    """Expose contention while preserving the production lock's ownership."""

    def __init__(self, lock, blocked):
        self.lock = lock
        self.blocked = blocked

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.blocked.set()
            if not self.lock.acquire(timeout=THREAD_TIMEOUT):
                raise AssertionError("The contended test lock was not released")
        return self

    def acquire(self, *, timeout):
        if self.lock.acquire(blocking=False):
            return True
        self.blocked.set()
        return self.lock.acquire(timeout=timeout)

    def release(self):
        self.lock.release()

    def __exit__(self, *_error):
        self.lock.release()
