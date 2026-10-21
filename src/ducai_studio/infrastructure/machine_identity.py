"""Versioned Windows installation identity; raw identifiers never leave this adapter."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from functools import lru_cache
from unicodedata import normalize
from uuid import UUID

from .offline_license import LicenseError, canonical_payload

REQUEST_PREFIX = "DUCAI-M1-"


def identity_digest(machine_guid: str, hardware_uuid: str, *, disk_serial: str = "") -> str:
    """Use firmware identity, or the system disk if firmware identifiers are absent."""
    try:
        install = UUID(machine_guid.strip().strip("{}"))
    except (ValueError, AttributeError) as exc:
        raise LicenseError(
            "Không đọc được định danh máy. Hãy thử lại hoặc liên hệ người cấp key."
        ) from exc
    if install.int in {0, (1 << 128) - 1}:
        raise LicenseError("Máy không cung cấp định danh hợp lệ. Hãy liên hệ người cấp key.")
    try:
        hardware = UUID(hardware_uuid.strip().strip("{}"))
    except (ValueError, AttributeError):
        hardware = UUID(int=0)
    if hardware.int not in {0, (1 << 128) - 1}:
        hardware_identity = {"kind": "smbios-uuid", "value": str(hardware)}
    else:
        serial = " ".join(normalize("NFKC", disk_serial).upper().split())
        compact = re.sub(r"[^A-Z0-9]", "", serial)
        if (len(compact) < 5 or len(serial) > 256 or len(set(compact)) < 2
                or compact in {"DEFAULTSTRING", "TOBEFILLEDBYOEM", "UNKNOWN", "NONE",
                               "SYSTEMSERIALNUMBER", "NOTSPECIFIED", "NOTAVAILABLE"}):
            raise LicenseError("Máy không cung cấp định danh hợp lệ. Hãy liên hệ người cấp key.")
        hardware_identity = {"kind": "system-disk-serial", "value": serial}
    return hashlib.sha256(canonical_payload({
        "domain": "ducai-studio-2/windows-v1", "install": str(install),
        "hardware": hardware_identity,
    })).hexdigest()


@lru_cache(maxsize=1)
def current_device_id() -> str:
    if os.name != "nt":
        raise LicenseError("Kích hoạt bản này yêu cầu Windows.")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        powershell = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop).UUID"],
            capture_output=True, text=True, timeout=10, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        hardware = result.stdout.strip()
        try:
            return identity_digest(guid, hardware)
        except LicenseError:
            # Many real Windows PCs have unset firmware UUIDs. The system disk's
            # manufacturer serial supplies a second, machine-specific identifier.
            disk = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                 "$ErrorActionPreference='Stop'; "
                 "$letter=$env:SystemDrive.TrimEnd(':'); "
                 "$disk=Get-Partition -DriveLetter $letter | Get-Disk | Select-Object -First 1; "
                 "@{serial=[string]$disk.SerialNumber} | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return identity_digest(guid, hardware, disk_serial=json.loads(disk.stdout)["serial"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise LicenseError(
            "Chưa đọc được mã máy. Bấm Kiểm tra lại; nếu vẫn lỗi hãy liên hệ hỗ trợ."
        ) from exc


def request_code(device_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", device_id):
        raise LicenseError("Mã máy không hợp lệ.")
    checksum = hashlib.sha256((REQUEST_PREFIX + device_id).encode("ascii")).hexdigest()[:8]
    return REQUEST_PREFIX + device_id + "-" + checksum


def parse_request_code(value: str) -> str:
    value = value.strip()
    if len(value) != len(REQUEST_PREFIX) + 64 + 9 or not value.startswith(REQUEST_PREFIX):
        raise LicenseError("Mã yêu cầu phải được sao chép đầy đủ từ Cài đặt → Kích hoạt.")
    device = value[len(REQUEST_PREFIX):len(REQUEST_PREFIX) + 64]
    if request_code(device) != value:
        raise LicenseError("Mã yêu cầu sai checksum. Hãy yêu cầu người dùng sao chép lại.")
    return device
