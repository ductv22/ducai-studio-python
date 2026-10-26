from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.tts import PiperTtsProvider, VieNeuTtsProvider, VoiceCatalog
from ducai_studio.providers.tts.catalog import inspect_voice_preview, valid_voice_preview

_CASTING_PREVIEW_TEXT = "Xin chào, đây là mẫu nghe thử giọng trong DUCAI Studio."


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _catalog_preview_ready(path: Path) -> bool:
    details = inspect_voice_preview(path)
    duration = float(details.get("duration_seconds", 0.0) or 0.0)
    return details.get("valid") is True and 5.0 <= duration <= 8.0


def _seed_preview(target: Path, seed_root: Path | None) -> bool:
    if seed_root is None:
        return False
    seed = seed_root / target.name
    if not _catalog_preview_ready(seed):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed, target)
    return True


def _inventory_row(
    *,
    voice_id: str,
    provider_id: str,
    name: str,
    path: Path,
    installed: bool,
    kind: str,
) -> dict[str, object]:
    details = inspect_voice_preview(path)
    return {
        "voice_id": voice_id,
        "provider_id": provider_id,
        "name": name,
        "kind": kind,
        "installed": installed,
        "path": str(path.resolve()),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": _sha256(path) if path.is_file() else "",
        **details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate exact local DUCAI casting auditions and their inventory"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--seed-existing",
        type=Path,
        help="Local catalog preview directory used only for an uninstalled official Piper voice",
    )
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    inventory_path = args.inventory.expanduser().resolve()
    seed_root = args.seed_existing.expanduser().resolve() if args.seed_existing else None
    output.mkdir(parents=True, exist_ok=True)
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = VoiceCatalog(data_root, preview_root=output)

    rows: list[dict[str, object]] = []
    vieneu = VieNeuTtsProvider(data_root=data_root, batch_size=1)
    try:
        presets = catalog.vieneu_presets(installed=catalog.vieneu_runtime_installed())
        for index, entry in enumerate(presets, 1):
            target = Path(entry.preview_path)
            if not _catalog_preview_ready(target):
                print(f"VieNeu preset {index}/{len(presets)}: {entry.name}", flush=True)
                vieneu.preview(
                    text=entry.preview_text,
                    voice_id=entry.voice_id,
                    output_path=target,
                )
            rows.append(
                _inventory_row(
                    voice_id=entry.voice_id,
                    provider_id=entry.provider_id,
                    name=entry.name,
                    path=target,
                    installed=entry.installed,
                    kind="preset",
                )
            )
    finally:
        vieneu.close()

    runtime_python = data_root / "piper_runtime" / ".venv" / "Scripts" / "python.exe"
    for entry in (item for item in catalog.entries() if item.installable):
        target = Path(entry.preview_path)
        if not _catalog_preview_ready(target):
            if entry.installed:
                piper = PiperTtsProvider(data_root=data_root, runtime_python=runtime_python)
                try:
                    print(f"Piper installed: {entry.name}", flush=True)
                    piper.preview(
                        text=entry.preview_text,
                        voice_id=entry.voice_id,
                        output_path=target,
                    )
                finally:
                    piper.close()
            elif not _seed_preview(target, seed_root):
                raise FileNotFoundError(
                    f"No local audition seed for uninstalled official voice {entry.voice_id}"
                )
        rows.append(
            _inventory_row(
                voice_id=entry.voice_id,
                provider_id=entry.provider_id,
                name=entry.name,
                path=target,
                installed=entry.installed,
                kind="piper",
            )
        )

    for index, profile in enumerate(catalog.vieneu_profiles(), 1):
        options = {
            "purpose": "preview",
            "preview_text": _CASTING_PREVIEW_TEXT,
            "batch_size": 1,
            "data_root": str(data_root),
            "profile_path": str(profile),
        }
        descriptor = JobManagerService.get_tts_voice_preview(
            provider_id="vieneu-3.8",
            voice_id=profile.stem,
            provider_options=options,
        )
        target = Path(str(descriptor["path"]))
        if not valid_voice_preview(target):
            print(f"VieNeu clone {index}: {profile.stem}", flush=True)
            clone = VieNeuTtsProvider(data_root=data_root, profile_path=profile, batch_size=1)
            try:
                clone.preview(
                    text=_CASTING_PREVIEW_TEXT,
                    voice_id=profile.stem,
                    output_path=target,
                )
            finally:
                clone.close()
        clone_row = _inventory_row(
            voice_id=profile.stem,
            provider_id="vieneu-3.8",
            name=f"Giọng clone {index} · {profile.stem[:8]}",
            path=target,
            installed=True,
            kind="clone",
        )
        clone_row["profile_path"] = str(profile)
        rows.append(clone_row)

    selectable = [row for row in rows if row["installed"] is True]
    for row in selectable:
        lookup_options: dict[str, object] = {
            "purpose": "preview",
            "preview_text": _CASTING_PREVIEW_TEXT,
            "batch_size": 1,
            "data_root": str(data_root),
        }
        if row["kind"] == "clone":
            lookup_options["profile_path"] = row["profile_path"]
        descriptor = JobManagerService.get_tts_voice_preview(
            provider_id=str(row["provider_id"]),
            voice_id=str(row["voice_id"]),
            provider_options=lookup_options,
        )
        row["lookup_available"] = descriptor["available"]
        row["lookup_path"] = descriptor["path"]
        row["preview_key"] = descriptor["preview_key"]
        row["voice_identity"] = descriptor["voice_identity"]
    payload = {
        "schema": "ducai-voice-preview-inventory-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "data_root": str(data_root),
        "catalog_output": str(output),
        "voice_count": len(rows),
        "installed_selectable_count": len(selectable),
        "valid_installed_selectable_count": sum(
            row.get("valid") is True and row.get("lookup_available") is True
            for row in selectable
        ),
        "voices": rows,
    }
    inventory_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if any(row.get("valid") is not True for row in rows):
        raise RuntimeError("One or more generated auditions are missing, silent, or invalid")
    if any(row.get("lookup_available") is not True for row in selectable):
        raise RuntimeError("An installed selectable voice is not immediately resolvable")
    if any(
        row["kind"] in {"preset", "piper"}
        and not 5.0 <= float(row.get("duration_seconds", 0.0) or 0.0) <= 8.0
        for row in rows
    ):
        raise RuntimeError("A bundled catalog audition is outside the 5–8 second contract")
    print(
        f"Verified {len(rows)} auditions ({len(selectable)} installed selectable); "
        f"inventory: {inventory_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
