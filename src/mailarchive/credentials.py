from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import NoReturn, Protocol
from uuid import uuid4


class CredentialError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def get(self, account_id: str) -> str | None: ...

    def set(self, account_id: str, password: str) -> None: ...

    def delete(self, account_id: str) -> None: ...


class MemoryCredentialStore:
    """In-memory storage for tests and embedded use; it does not persist data."""

    def __init__(self) -> None:
        self._passwords: dict[str, str] = {}

    def get(self, account_id: str) -> str | None:
        return self._passwords.get(account_id)

    def set(self, account_id: str, password: str) -> None:
        self._passwords[account_id] = password

    def delete(self, account_id: str) -> None:
        self._passwords.pop(account_id, None)


class UnavailableCredentialStore:
    """Keeps the UI usable without falling back to insecure credential storage."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _raise(self) -> NoReturn:
        raise CredentialError(self.reason)

    def get(self, account_id: str) -> str | None:
        self._raise()

    def set(self, account_id: str, password: str) -> None:
        self._raise()

    def delete(self, account_id: str) -> None:
        self._raise()


class KeyringCredentialStore:
    """Secure system keyring, including Secret Service and KWallet on Linux."""

    def __init__(
        self, service_name: str = "MailArchive", keyring_module: object | None = None
    ) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module
            except ImportError as exc:
                raise CredentialError("The Python package 'keyring' is not installed.") from exc
        self._keyring = keyring_module
        self.service_name = service_name
        try:
            backend = self._keyring.get_keyring()  # type: ignore[attr-defined]
            priority = backend.priority
            if callable(priority):
                priority = priority()
            if float(priority) <= 0:
                raise CredentialError(
                    "No secure system keyring is available. Install and unlock Secret Service "
                    "(GNOME Keyring) or KWallet."
                )
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"The system keyring is unavailable: {exc}") from exc

    def get(self, account_id: str) -> str | None:
        try:
            return self._keyring.get_password(self.service_name, account_id)  # type: ignore[attr-defined]
        except Exception as exc:
            raise CredentialError(f"Could not read the password: {exc}") from exc

    def set(self, account_id: str, password: str) -> None:
        try:
            self._keyring.set_password(self.service_name, account_id, password)  # type: ignore[attr-defined]
        except Exception as exc:
            raise CredentialError(f"Could not store the password: {exc}") from exc

    def delete(self, account_id: str) -> None:
        try:
            if self.get(account_id) is not None:
                self._keyring.delete_password(self.service_name, account_id)  # type: ignore[attr-defined]
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"Could not delete the password: {exc}") from exc


if os.name == "nt":

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialStore:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
    UTF8_BLOB_PREFIX = b"MailArchive-UTF8\0"
    LEGACY_CHUNK_MANIFEST_PREFIX = b"MailArchive-Chunks-v1\0"
    CHUNK_MANIFEST_PREFIX = b"MailArchive-Chunks-v2\0"
    MAX_CREDENTIAL_CHUNKS = 128

    def __init__(self, prefix: str = "MailArchive") -> None:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager is only available on Windows.")
        self.prefix = prefix
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def _target(self, account_id: str) -> str:
        return f"{self.prefix}/{account_id}"

    def _chunk_target(self, account_id: str, generation: str, index: int) -> str:
        return f"{self._target(account_id)}/chunk/{generation}/{index}"

    @classmethod
    def _encode_value(cls, value: str) -> bytes:
        return cls.UTF8_BLOB_PREFIX + value.encode("utf-8")

    @classmethod
    def _decode_value(cls, value: bytes) -> str:
        if value.startswith(cls.UTF8_BLOB_PREFIX):
            return value[len(cls.UTF8_BLOB_PREFIX) :].decode("utf-8")
        # Versions before application credentials stored UTF-16LE without a marker.
        return value.decode("utf-16-le")

    @classmethod
    def _manifest(
        cls,
        active: tuple[str, int],
        stale: tuple[tuple[str, int], ...] = (),
    ) -> bytes:
        def encode_chunk_set(chunk_set: tuple[str, int]) -> str:
            generation, count = chunk_set
            return f"{generation}:{count}"

        active_text = encode_chunk_set(active)
        stale_text = ",".join(encode_chunk_set(chunk_set) for chunk_set in stale)
        return cls.CHUNK_MANIFEST_PREFIX + f"{active_text}|{stale_text}".encode("ascii")

    @classmethod
    def _parse_chunk_set(cls, value: str) -> tuple[str, int]:
        try:
            generation, count_text = value.split(":", 1)
            count = int(count_text)
        except ValueError as exc:
            raise CredentialError("The stored Windows credential manifest is invalid.") from exc
        if (
            len(generation) != 32
            or any(character not in "0123456789abcdef" for character in generation)
            or not 1 <= count <= cls.MAX_CREDENTIAL_CHUNKS
        ):
            raise CredentialError("The stored Windows credential manifest is invalid.")
        return generation, count

    @classmethod
    def _parse_manifest(
        cls,
        value: bytes,
    ) -> tuple[tuple[str, int], tuple[tuple[str, int], ...]] | None:
        if value.startswith(cls.LEGACY_CHUNK_MANIFEST_PREFIX):
            try:
                chunk_set_text = value[len(cls.LEGACY_CHUNK_MANIFEST_PREFIX) :].decode("ascii")
            except UnicodeError as exc:
                raise CredentialError("The stored Windows credential manifest is invalid.") from exc
            return cls._parse_chunk_set(chunk_set_text), ()
        if not value.startswith(cls.CHUNK_MANIFEST_PREFIX):
            return None
        try:
            active_text, stale_text = (
                value[len(cls.CHUNK_MANIFEST_PREFIX) :].decode("ascii").split("|", 1)
            )
        except (UnicodeError, ValueError) as exc:
            raise CredentialError("The stored Windows credential manifest is invalid.") from exc
        active = cls._parse_chunk_set(active_text)
        stale = tuple(
            cls._parse_chunk_set(chunk_set_text)
            for chunk_set_text in stale_text.split(",")
            if chunk_set_text
        )
        return active, stale

    def _read_blob(self, target: str) -> bytes | None:
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not self._advapi.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"Could not read the password (Windows error {error}).")
        try:
            credential = pointer.contents
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            self._advapi.CredFree(pointer)

    def _write_blob(self, target: str, value: bytes) -> None:
        if len(value) > self.MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialError(
                "The credential chunk is too large for Windows Credential Manager."
            )
        blob = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        credential = CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = target
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError(
                f"Could not store the password (Windows error {ctypes.get_last_error()})."
            )

    def _delete_target(self, target: str) -> None:
        if not self._advapi.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != self.ERROR_NOT_FOUND:
                raise CredentialError(f"Could not delete the password (Windows error {error}).")

    def _delete_chunks(self, account_id: str, chunk_set: tuple[str, int]) -> None:
        generation, count = chunk_set
        first_error: CredentialError | None = None
        for index in range(count):
            try:
                self._delete_target(self._chunk_target(account_id, generation, index))
            except CredentialError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def get(self, account_id: str) -> str | None:
        value = self._read_blob(self._target(account_id))
        if value is None:
            return None
        manifest = self._parse_manifest(value)
        if manifest is None:
            return self._decode_value(value)
        (generation, count), _stale = manifest
        chunks: list[bytes] = []
        for index in range(count):
            chunk = self._read_blob(self._chunk_target(account_id, generation, index))
            if chunk is None:
                raise CredentialError("The stored Windows credential is incomplete.")
            chunks.append(chunk)
        return self._decode_value(b"".join(chunks))

    def set(self, account_id: str, password: str) -> None:
        target = self._target(account_id)
        existing = self._read_blob(target)
        existing_manifest = self._parse_manifest(existing) if existing is not None else None
        encoded = self._encode_value(password)
        if len(encoded) <= self.MAX_CREDENTIAL_BLOB_SIZE and existing_manifest is None:
            self._write_blob(target, encoded)
            return

        chunks = [
            encoded[offset : offset + self.MAX_CREDENTIAL_BLOB_SIZE]
            for offset in range(0, len(encoded), self.MAX_CREDENTIAL_BLOB_SIZE)
        ]
        if len(chunks) > self.MAX_CREDENTIAL_CHUNKS:
            raise CredentialError("The credential is too large for Windows Credential Manager.")
        active = (uuid4().hex, len(chunks))
        stale = (
            (existing_manifest[0], *existing_manifest[1]) if existing_manifest is not None else ()
        )
        written_targets: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                chunk_target = self._chunk_target(account_id, active[0], index)
                self._write_blob(chunk_target, chunk)
                written_targets.append(chunk_target)
            self._write_blob(target, self._manifest(active, stale))
        except Exception:
            for chunk_target in written_targets:
                try:
                    self._delete_target(chunk_target)
                except CredentialError:
                    pass
            raise

        try:
            for stale_chunk_set in stale:
                self._delete_chunks(account_id, stale_chunk_set)
        except CredentialError:
            # Keep the published manifest with stale-generation references so a
            # later replace/delete can retry cleanup without losing the current value.
            raise
        self._write_blob(target, self._manifest(active))

    def delete(self, account_id: str) -> None:
        target = self._target(account_id)
        existing = self._read_blob(target)
        if existing is None:
            return
        manifest = self._parse_manifest(existing)
        if manifest is not None:
            active, stale = manifest
            for chunk_set in (active, *stale):
                self._delete_chunks(account_id, chunk_set)
        self._delete_target(target)
