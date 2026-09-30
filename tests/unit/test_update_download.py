import hashlib
import io
import zipfile

import pytest

from ducai_studio.infrastructure.update_download import (
    UpdateCancelled,
    UpdateDownloadError,
    download_verified_archive,
    extract_verified_archive,
)
from ducai_studio.infrastructure.update_manifest import (
    canonical_manifest_bytes,
    expected_archive_name,
    parse_manifest,
)

VERSION = "2.0.0a118"
URL = (
    "https://github.com/ductv22/ducai-studio-releases/releases/download/"
    f"v{VERSION}/{expected_archive_name(VERSION)}"
)


def make_zip(entries, *, symlink=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            if symlink:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, content)
            else:
                archive.writestr(name, content)
    return output.getvalue()


def manifest_for(data, entries):
    payload = {
        "schema": 1,
        "product": "DUCAI STUDIO",
        "version": VERSION,
        "channel": "testing",
        "platform": "win64",
        "archive": {
            "name": expected_archive_name(VERSION),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "files": [
            {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(entries.items())
        ],
    }
    return parse_manifest(canonical_manifest_bytes(payload))


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)

    def geturl(self):
        return URL


def test_streamed_download_progress_and_exact_stage(tmp_path):
    entries = {"DucaiStudio2.exe": b"exe", "ui/main.qml": b"hello"}
    data = make_zip(entries)
    manifest = manifest_for(data, entries)
    destination = tmp_path / manifest.archive.name
    progress = []
    download_verified_archive(
        URL,
        destination,
        manifest,
        progress=lambda received, total: progress.append((received, total)),
        opener=lambda request, timeout: Response(data),
    )
    assert destination.read_bytes() == data
    assert progress[-1] == (len(data), len(data))
    stage = extract_verified_archive(destination, tmp_path / "stage", manifest)
    assert (stage / "DucaiStudio2.exe").read_bytes() == b"exe"
    assert (stage / "ui" / "main.qml").read_bytes() == b"hello"


@pytest.mark.parametrize("body", [b"too short", b"x" * 1000])
def test_tampered_or_short_download_does_not_publish(tmp_path, body):
    entries = {"DucaiStudio2.exe": b"exe"}
    data = make_zip(entries)
    manifest = manifest_for(data, entries)
    destination = tmp_path / manifest.archive.name
    with pytest.raises(UpdateDownloadError):
        download_verified_archive(
            URL, destination, manifest, opener=lambda request, timeout: Response(body)
        )
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_cancellation_cleans_partial_file(tmp_path):
    entries = {"DucaiStudio2.exe": b"exe"}
    data = make_zip(entries)
    manifest = manifest_for(data, entries)
    destination = tmp_path / manifest.archive.name
    with pytest.raises(UpdateCancelled):
        download_verified_archive(
            URL,
            destination,
            manifest,
            cancelled=lambda: True,
            opener=lambda request, timeout: Response(data),
        )
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


@pytest.mark.parametrize(
    "archive_entries, signed_entries",
    [
        ({"DucaiStudio2.exe": b"exe", "extra": b"x"}, {"DucaiStudio2.exe": b"exe"}),
        ({"DucaiStudio2.exe": b"wrong"}, {"DucaiStudio2.exe": b"exe"}),
        ({"DucaiStudio2.exe": b"exe", "../escape": b"x"}, {"DucaiStudio2.exe": b"exe"}),
    ],
)
def test_archive_mismatch_never_publishes_stage(tmp_path, archive_entries, signed_entries):
    data = make_zip(archive_entries)
    manifest = manifest_for(data, signed_entries)
    path = tmp_path / manifest.archive.name
    path.write_bytes(data)
    with pytest.raises(UpdateDownloadError):
        extract_verified_archive(path, tmp_path / "stage", manifest)
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "escape").exists()


def test_symlink_archive_entry_rejected(tmp_path):
    entries = {"DucaiStudio2.exe": b"exe"}
    data = make_zip(entries, symlink=True)
    manifest = manifest_for(data, entries)
    path = tmp_path / manifest.archive.name
    path.write_bytes(data)
    with pytest.raises(UpdateDownloadError, match="link"):
        extract_verified_archive(path, tmp_path / "stage", manifest)
