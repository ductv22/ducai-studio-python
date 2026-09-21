from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_TARGET_PREFIX = "DUCAI_STUDIO/"


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Store only provider secrets in the current Windows user's vault."""

    def __init__(self, *, target_prefix: str = _TARGET_PREFIX) -> None:
        if os.name != "nt":
            raise OSError("Windows Credential Manager is only available on Windows")
        if not target_prefix.strip():
            raise ValueError("Credential target prefix is required")
        self.target_prefix = target_prefix
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        credential_array = ctypes.POINTER(ctypes.POINTER(_Credential))
        self._advapi.CredEnumerateW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(credential_array),
        ]
        self._advapi.CredEnumerateW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [wintypes.LPVOID]
        self._advapi.CredFree.restype = None

    def _target(self, reference: str) -> str:
        value = reference.strip()
        if not value or any(character in value for character in "\r\n\0"):
            raise ValueError("Credential reference is invalid")
        return self.target_prefix + value

    def write(self, reference: str, secret: str, *, username: str = "api-key") -> None:
        if not secret or "\0" in secret:
            raise ValueError("Credential secret is required")
        blob = secret.encode("utf-16-le")
        if len(blob) > 5_120:
            raise ValueError("Credential secret is too large")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(
            0,
            _CRED_TYPE_GENERIC,
            self._target(reference),
            "DUCAI STUDIO provider credential",
            wintypes.FILETIME(),
            len(blob),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            _CRED_PERSIST_LOCAL_MACHINE,
            0,
            None,
            None,
            username,
        )
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, reference: str) -> str:
        pointer = ctypes.POINTER(_Credential)()
        if not self._advapi.CredReadW(
            self._target(reference), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == 1168:
                raise LookupError(f"Credential reference does not exist: {reference}")
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            blob = ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            )
            return blob.decode("utf-16-le")
        finally:
            self._advapi.CredFree(pointer)

    def delete(self, reference: str) -> bool:
        if self._advapi.CredDeleteW(self._target(reference), _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == 1168:
            return False
        raise ctypes.WinError(error)

    def list_references(self) -> tuple[str, ...]:
        count = wintypes.DWORD()
        credentials = ctypes.POINTER(ctypes.POINTER(_Credential))()
        if not self._advapi.CredEnumerateW(
            self.target_prefix + "*", 0, ctypes.byref(count), ctypes.byref(credentials)
        ):
            error = ctypes.get_last_error()
            if error == 1168:
                return ()
            raise ctypes.WinError(error)
        try:
            references: dict[str, str] = {}
            prefix_folded = self.target_prefix.casefold()
            for index in range(count.value):
                target = credentials[index].contents.TargetName or ""
                if not target.casefold().startswith(prefix_folded):
                    continue
                reference = target[len(self.target_prefix) :].strip()
                if reference:
                    references.setdefault(reference.casefold(), reference)
            return tuple(sorted(references.values(), key=str.casefold))
        finally:
            self._advapi.CredFree(credentials)
