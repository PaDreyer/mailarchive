from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import NoReturn, Protocol


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

    def __init__(self, service_name: str = "MailArchive", keyring_module: object | None = None) -> None:
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

    @classmethod
    def _encode_value(cls, value: str) -> bytes:
        encoded = cls.UTF8_BLOB_PREFIX + value.encode("utf-8")
        if len(encoded) > cls.MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialError(
                "The credential is too large for Windows Credential Manager."
            )
        return encoded

    @classmethod
    def _decode_value(cls, value: bytes) -> str:
        if value.startswith(cls.UTF8_BLOB_PREFIX):
            return value[len(cls.UTF8_BLOB_PREFIX) :].decode("utf-8")
        # Versions before application credentials stored UTF-16LE without a marker.
        return value.decode("utf-16-le")

    def get(self, account_id: str) -> str | None:
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not self._advapi.CredReadW(
            self._target(account_id), self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"Could not read the password (Windows error {error}).")
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return self._decode_value(blob)
        finally:
            self._advapi.CredFree(pointer)

    def set(self, account_id: str, password: str) -> None:
        encoded = self._encode_value(password)
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self._target(account_id)
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = account_id
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError(
                f"Could not store the password (Windows error {ctypes.get_last_error()})."
            )

    def delete(self, account_id: str) -> None:
        if not self._advapi.CredDeleteW(self._target(account_id), self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != self.ERROR_NOT_FOUND:
                raise CredentialError(
                    f"Could not delete the password (Windows error {error})."
                )
