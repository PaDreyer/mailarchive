"""Shared bounds for downloading and staging raw messages."""


class IntakeCapacityError(RuntimeError):
    """A raw-message transfer cannot fit within the configured local bounds."""


class SpoolCapacityError(IntakeCapacityError):
    """Admission must stop until local work storage is available."""


class MessageTooLargeError(IntakeCapacityError):
    """One provider message permanently exceeds the supported intake size."""


class IntakeQueueCapacityError(IntakeCapacityError):
    """Admission must stop until unresolved message intakes are handled."""


MESSAGE_CHUNK_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 256 * 1024**2
MAX_SPOOL_BYTES = 2 * 1024**3
DISK_RESERVE_BYTES = 64 * 1024**2
MAX_JSON_BYTES = 8 * 1024**2
MAX_GMAIL_WIRE_BYTES = (MAX_MESSAGE_BYTES * 4 // 3) + 2 * MESSAGE_CHUNK_BYTES
MAX_ACTIVE_INTAKES = 256
