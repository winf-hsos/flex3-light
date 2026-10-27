"""API keys in the Windows Credential Manager, never in settings.json or the log.

Each service has one generic credential, "FLEX3 Light/<service>", stored for the
current user on this computer. The window only ever learns whether a key is there;
the key itself goes in once, from the settings, and is read back only by the code
that calls the service.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

PREFIX = "FLEX3 Light/"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2          # this user on this computer, not roaming
ERROR_NOT_FOUND = 1168


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR)]


_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_advapi.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
_advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wt.DWORD]
_advapi.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
_advapi.CredFree.argtypes = [ctypes.c_void_p]


def get(service: str) -> str | None:
    p = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(PREFIX + service, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        c = p.contents
        return bytes(c.CredentialBlob[:c.CredentialBlobSize]).decode("utf-8")
    finally:
        _advapi.CredFree(p)


def put(service: str, secret: str) -> None:
    data = secret.encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = _CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=PREFIX + service, CredentialBlobSize=len(data),
                       CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="FLEX3 Light")
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete(service: str) -> None:
    if not _advapi.CredDeleteW(PREFIX + service, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
        raise ctypes.WinError(ctypes.get_last_error())


def has(service: str) -> bool:
    return get(service) is not None
