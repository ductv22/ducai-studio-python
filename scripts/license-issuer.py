"""Owner-only license issuer. Never include this script or its state in customer builds."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.credential_store import WindowsCredentialStore
from ducai_studio.infrastructure.machine_identity import parse_request_code
from ducai_studio.infrastructure.offline_license import (
    LICENSE_KEY_ID,
    LICENSE_PUBLIC_KEYS,
    PRODUCT,
    TOKEN_PREFIX,
    canonical_payload,
    encode_base64,
    encode_compact_license,
    encode_expiring_license,
    verify_license,
)


def owner_store() -> WindowsCredentialStore:
    # Separate namespace: the customer API-key manager must not enumerate this key.
    return WindowsCredentialStore(target_prefix="DUCAI_LICENSE_ISSUER/")


def private_key() -> Ed25519PrivateKey:
    try:
        secret = owner_store().read(LICENSE_KEY_ID)
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret, validate=True))
    except (LookupError, ValueError) as exc:
        raise ValueError("Không tìm thấy khóa cấp key của tài khoản Windows này.") from exc


def public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes_raw().hex()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def initialize(root: Path) -> str:
    store = owner_store()
    try:
        key = private_key()
    except ValueError:
        if LICENSE_PUBLIC_KEYS.get(LICENSE_KEY_ID) is not None:
            raise ValueError("Máy này chưa có khóa cấp key của bản tool. Hãy khôi phục từ "
                             "file sao lưu đã mã hóa; không tạo khóa thay thế.") from None
        if (root / "key-info.json").exists():
            raise ValueError("Đã có thông tin khóa cũ nhưng vault không đọc được. Hãy khôi phục "
                             "từ bản sao lưu; không tạo khóa khác cho cùng bộ cấp key.") from None
        key = Ed25519PrivateKey.generate()
        store.write(LICENSE_KEY_ID, base64.b64encode(key.private_bytes_raw()).decode("ascii"),
                    username="DUCAI offline licensing owner")
    public = public_hex(key)
    pinned = LICENSE_PUBLIC_KEYS.get(LICENSE_KEY_ID)
    if pinned is not None and public != pinned.hex():
        raise ValueError("Khóa cấp key không khớp bản tool này. Hãy khôi phục đúng khóa đã "
                         "sao lưu; không cấp key từ một khóa mới.")
    info = root / "key-info.json"
    if info.exists() and json.loads(info.read_text(encoding="utf-8")).get("public_key") != public:
        raise ValueError("Khóa trong vault không khớp bộ cấp key. Không ghi đè thông tin khóa.")
    atomic_json(info, {"product": PRODUCT, "key_id": LICENSE_KEY_ID, "public_key": public,
                       "private_storage": "Windows Credential Manager/current user"})
    return public


def issue_token(
    request: str,
    key: Ed25519PrivateKey,
    *,
    legacy: bool = False,
    duration_hours: int | None = None,
    now: datetime | None = None,
) -> tuple[str, dict]:
    if duration_hours is not None:
        if isinstance(duration_hours, bool) or not isinstance(duration_hours, int) \
                or duration_hours <= 0:
            raise ValueError("Thời hạn phải là số giờ nguyên dương.")
        if legacy:
            raise ValueError("Key có thời hạn chỉ dùng định dạng L3; không thể kết hợp --legacy.")
    issued = datetime.now(UTC) if now is None else now
    issued = (issued.replace(tzinfo=UTC) if issued.tzinfo is None
              else issued.astimezone(UTC))
    issued_at = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
    device = parse_request_code(request)
    payload = {
        "schema": 1 if legacy else (3 if duration_hours is not None else 2),
        "product": PRODUCT, "key_id": LICENSE_KEY_ID,
        "license_id": uuid4().hex,
        "device_schema": "windows-v1", "device_id": device, "edition": "full",
        "issued_at": issued_at,
    }
    if duration_hours is not None:
        payload["expires_at"] = (
            issued + timedelta(hours=duration_hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = key.sign(canonical_payload(payload))
    if legacy:
        envelope = {"payload": payload, "signature": encode_base64(signature)}
        token = TOKEN_PREFIX + encode_base64(canonical_payload(envelope))
    elif duration_hours is not None:
        token = encode_expiring_license(payload, signature)
    else:
        token = encode_compact_license(payload, signature)
    verify_license(token, device_id=device,
                   public_keys={LICENSE_KEY_ID: key.public_key().public_bytes_raw()},
                   now=issued)
    return token, payload


@contextmanager
def issuer_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "issuer.lock"
    try:
        with path.open("x", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError as exc:
        raise ValueError(
            "Bộ cấp key khác đang thao tác. Đóng bộ đó rồi thử lại; "
            "nếu mất điện giữa thao tác, kiểm tra sổ trước khi xóa issuer.lock."
        ) from exc
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def issue(
    root: Path,
    request: str,
    destination: Path,
    *,
    legacy: bool = False,
    duration_hours: int | None = None,
) -> str:
    with issuer_lock(root):
        initialize(root)
        token, payload = issue_token(
            request, private_key(), legacy=legacy, duration_hours=duration_hours
        )
        ledger_path = root / "issued-licenses.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else []
        if not isinstance(ledger, list):
            raise ValueError("Sổ cấp key không hợp lệ; hãy khôi phục trước khi cấp key mới.")
        # Record before publishing; a failed file write never loses issuance history.
        atomic_json(ledger_path, [*ledger, payload])
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    return token


def backup(destination: Path, password: str) -> None:
    if len(password) < 12:
        raise ValueError("Mật khẩu sao lưu cần ít nhất 12 ký tự.")
    pem = private_key().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    with destination.open("xb") as stream:
        stream.write(pem)
        stream.flush()
        os.fsync(stream.fileno())


def restore(root: Path, source: Path, password: str) -> str:
    with source.open("rb") as stream:
        data = stream.read(16_385)
    if len(data) > 16_384:
        raise ValueError("File sao lưu quá lớn hoặc không hợp lệ.")
    key = serialization.load_pem_private_key(data, password=password.encode("utf-8"))
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Bản sao lưu không phải khóa Ed25519.")
    if key.public_key().public_bytes_raw() != LICENSE_PUBLIC_KEYS.get(LICENSE_KEY_ID):
        raise ValueError("Bản sao lưu không khớp khóa mà tool đang tin cậy.")
    store = owner_store()
    try:
        existing = private_key()
    except ValueError:
        existing = None
    if existing is not None and public_hex(existing) != public_hex(key):
        raise ValueError("Không ghi đè khóa cấp key đang có bằng một khóa khác.")
    info = root / "key-info.json"
    if (info.exists()
            and json.loads(info.read_text(encoding="utf-8")).get("public_key") != public_hex(key)):
        raise ValueError("Bản sao lưu không khớp khóa được ghi nhận trong bộ cấp key.")
    store.write(LICENSE_KEY_ID, base64.b64encode(key.private_bytes_raw()).decode("ascii"),
                username="DUCAI offline licensing owner")
    return initialize(root)


def gui(root: Path) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    window = tk.Tk()
    window.title("DUCAI — Cấp key offline (chỉ dành cho chủ tool)")
    window.geometry("780x590")
    window.minsize(620, 500)
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="CẤP KEY DUCAI STUDIO", font=("Segoe UI", 17, "bold")).pack(anchor="w")
    ttk.Label(frame, text="Dán mã máy khách gửi từ Cài đặt → Kích hoạt. Chọn key vĩnh viễn "
              "hoặc key có thời hạn cho nhóm test; mọi key đều chỉ dành cho máy đó.",
              wraplength=730).pack(anchor="w", pady=(8, 12))
    ttk.Label(frame, text="Mã máy DUCAI-M1-…").pack(anchor="w")
    request = tk.Text(frame, height=3, wrap="char", font=("Consolas", 11))
    request.pack(fill="x", pady=6)
    issuance_mode = tk.StringVar(value="perpetual")
    mode_frame = ttk.LabelFrame(frame, text="Thời hạn key")
    mode_frame.pack(fill="x", pady=(4, 0))
    ttk.Radiobutton(mode_frame, text="Vĩnh viễn (L2)", variable=issuance_mode,
                    value="perpetual").pack(side="left", padx=(8, 18), pady=6)
    ttk.Radiobutton(mode_frame, text="Có thời hạn (L3)", variable=issuance_mode,
                    value="timed").pack(side="left", padx=(0, 8), pady=6)
    ttk.Label(mode_frame, text="Số giờ:").pack(side="left", padx=(10, 4))
    duration_entry = ttk.Entry(mode_frame, width=9)
    duration_entry.insert(0, "168")
    duration_entry.pack(side="left", padx=(0, 8), pady=6)
    ttk.Label(mode_frame, text="(ví dụ 168 = 7 ngày)").pack(side="left", padx=(0, 8))
    legacy = tk.BooleanVar(value=False)
    legacy_check = ttk.Checkbutton(frame, text="Key dài cho a139 (chỉ dùng cho key vĩnh viễn)",
                                    variable=legacy)
    legacy_check.pack(anchor="w", pady=(4, 0))

    def update_legacy_mode(*_args: object) -> None:
        if issuance_mode.get() == "timed":
            legacy.set(False)
            legacy_check.state(["disabled"])
        else:
            legacy_check.state(["!disabled"])

    issuance_mode.trace_add("write", update_legacy_mode)
    update_legacy_mode()
    ttk.Label(frame, text="Key đã cấp — gửi file hoặc sao chép key cho khách").pack(
        anchor="w", pady=(10, 0))
    result = tk.Text(frame, height=6, wrap="char", font=("Consolas", 10))
    result.pack(fill="both", expand=True, pady=6)
    status = tk.StringVar(value="Khóa riêng lưu trong vault Windows; "
                          "không gửi bộ cấp key này cho khách.")

    def create() -> None:
        try:
            request_value = request.get("1.0", "end").strip()
            parse_request_code(request_value)
            duration_hours = None
            if issuance_mode.get() == "timed":
                try:
                    duration_hours = int(duration_entry.get().strip())
                except ValueError as exc:
                    raise ValueError("Số giờ phải là số nguyên dương.") from exc
                if duration_hours <= 0:
                    raise ValueError("Số giờ phải là số nguyên dương.")
            if legacy.get() and duration_hours is not None:
                raise ValueError("Key a139 không hỗ trợ thời hạn; hãy chọn L2 hoặc bỏ legacy.")
            path = filedialog.asksaveasfilename(defaultextension=".ducai-license",
                        initialfile=f"DUCAI-{uuid4().hex[:8]}.ducai-license",
                        filetypes=[("DUCAI key", "*.ducai-license")])
            if not path:
                return
            token = issue(root, request_value, Path(path),
                          legacy=legacy.get(), duration_hours=duration_hours)
            result.delete("1.0", "end")
            result.insert("1.0", token)
            if duration_hours is None:
                kind = "dài cho a139" if legacy.get() else "vĩnh viễn L2"
            else:
                kind = f"có thời hạn L3 ({duration_hours} giờ)"
            status.set(f"Đã cấp key {kind} ({len(token)} ký tự) và ghi sổ. "
                       "Gửi file key vừa lưu cho khách.")
        except Exception as exc:
            messagebox.showerror("Không cấp được key", str(exc))

    def copy() -> None:
        value = result.get("1.0", "end").strip()
        if value:
            window.clipboard_clear()
            window.clipboard_append(value)
            status.set("Đã sao chép key.")

    def save_backup() -> None:
        path = filedialog.asksaveasfilename(defaultextension=".pem",
                                           initialfile="ducai-license-key-backup.pem")
        if not path:
            return
        password = simpledialog.askstring("Mật khẩu sao lưu", "Ít nhất 12 ký tự; hãy lưu mật khẩu "
                                          "ở nơi an toàn, tách khỏi file sao lưu.", show="*")
        if not password:
            return
        confirm = simpledialog.askstring("Xác nhận", "Nhập lại mật khẩu sao lưu", show="*")
        if confirm != password:
            messagebox.showerror("Không khớp", "Hai mật khẩu không trùng nhau.")
            return
        try:
            backup(Path(path), password)
            status.set("Đã sao lưu khóa có mã hóa. Không gửi file này cho khách.")
        except Exception as exc:
            messagebox.showerror("Không sao lưu được", str(exc))

    def load_backup() -> None:
        path = filedialog.askopenfilename(filetypes=[("Encrypted key backup", "*.pem")])
        if not path:
            return
        password = simpledialog.askstring("Khôi phục khóa", "Mật khẩu của file sao lưu", show="*")
        if not password:
            return
        try:
            restore(root, Path(path), password)
            status.set("Đã khôi phục khóa cấp key.")
        except Exception:
            messagebox.showerror("Không khôi phục được", "Sai mật khẩu, file không hợp lệ hoặc "
                                  "khóa không khớp. Khóa đang có không bị thay thế.")

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=10)
    for label, callback in [("Cấp và lưu key", create), ("Sao chép key", copy),
                            ("Sao lưu khóa riêng", save_backup), ("Khôi phục khóa", load_backup)]:
        ttk.Button(buttons, text=label, command=callback).pack(side="left", padx=(0, 8))
    ttk.Label(frame, textvariable=status, wraplength=730).pack(anchor="w")
    window.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Owner-only DUCAI offline license issuer")
    parser.add_argument("--state-root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", "."))
                        / "DUCAI License Issuer")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init")
    issue_parser = commands.add_parser("issue")
    issue_parser.add_argument("--request-code", required=True)
    issue_parser.add_argument("--output", type=Path, required=True)
    issue_parser.add_argument("--legacy", action="store_true",
                              help="Issue a long DUCAI-L1 key for customers still on a139")
    issue_parser.add_argument("--duration-hours", type=int,
                              help="Issue a DUCAI-L3 key that expires after this many hours")
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--output", type=Path, required=True)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            print(initialize(args.state_root))  # Public verification bytes only.
        elif args.command == "issue":
            kwargs = {"legacy": args.legacy}
            if args.duration_hours is not None:
                kwargs["duration_hours"] = args.duration_hours
            issue(args.state_root, args.request_code, args.output, **kwargs)
            print("License file created.")
        elif args.command == "backup":
            password = getpass.getpass("Backup password (12+ characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
            backup(args.output, password)
        elif args.command == "restore":
            restore(args.state_root, args.input, getpass.getpass("Backup password: "))
        else:
            gui(args.state_root)
    except Exception:
        sys.stderr.write("Owner operation failed; no customer activation or project changed.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
