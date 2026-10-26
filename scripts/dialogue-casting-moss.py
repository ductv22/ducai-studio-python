"""Explicit MOSS installer/verifier; inference is always offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dialogue_casting.moss import install_moss_assets, verify_moss_assets, verify_moss_runtime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "verify"))
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    if args.action == "install":
        install_moss_assets(root / "model", root / "source", args.cache or root / "downloads",
                            offline=args.offline,
                            legacy_root=(args.legacy_root.resolve()
                                         if args.legacy_root is not None else None))
    lock = verify_moss_assets(root / "model", root / "source")
    print(json.dumps({"status": "PASS", "model_revision": lock["model_revision"],
                      "runtime": verify_moss_runtime()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
