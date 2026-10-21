"""Fail customer packaging on issuer/private/activation data, or an unpinned trust root."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from ducai_studio.infrastructure.offline_license import LICENSE_KEY_ID, LICENSE_PUBLIC_KEYS
from ducai_studio.infrastructure.update_manifest import PUBLIC_KEY_BYTES

PRIVATE_NAMES = frozenset({
    "license-issuer.py", "license-issuer.exe", "ducai-key-issuer.exe", "key-info.json",
    "issued-licenses.json", "license-v1.txt", "sitecustomize.py",
})
PRIVATE_SUFFIXES = frozenset({".pem", ".key", ".ducai-license", ".whl"})
PRIVATE_PEM = re.compile(
    rb"-----BEGIN (?:ENCRYPTED |ED25519 |RSA |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{20}")
LICENSE_TOKEN = re.compile(
    rb"(?:DUCAI-L1\.[A-Za-z0-9_-]{200,}|DUCAI-L2\.[A-Za-z0-9_-]{119}|"
    rb"DUCAI-L3\.[A-Za-z0-9_-]{130})"
)


def check_release(root: Path | None = None) -> None:
    public = LICENSE_PUBLIC_KEYS.get(LICENSE_KEY_ID)
    if public is None or len(public) != 32 or public == PUBLIC_KEY_BYTES or public == bytes(32):
        raise ValueError("License trust root missing, invalid or reused from the updater")
    if root is None:
        return
    if not (root / "DucaiStudio2.exe").is_file():
        raise ValueError("Customer executable is missing")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if (path.name.casefold() in PRIVATE_NAMES or path.suffix.casefold() in PRIVATE_SUFFIXES
                or "license-bootstrap" in str(path).casefold()):
            raise ValueError(f"Owner/test/activation material in customer package: {path.name}")
        with path.open("rb") as stream:
            previous = b""
            while chunk := stream.read(1024 * 1024):
                block = previous + chunk
                if PRIVATE_PEM.search(block) or LICENSE_TOKEN.search(block):
                    raise ValueError(f"Private key or personal activation token in {path.name}")
                previous = block[-8192:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path)
    arguments = parser.parse_args()
    check_release(arguments.release)
    print("License packaging boundary verified.")


if __name__ == "__main__":
    main()
