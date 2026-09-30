import hashlib
import io
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ducai_studio.infrastructure.github_updates import (
    GitHubUpdateClient,
    UpdateCheckError,
    parse_release,
    select_release,
    validate_https_url,
)
from ducai_studio.infrastructure.update_manifest import (
    canonical_manifest_bytes,
    expected_archive_name,
)


def asset(name, version, data=b"data"):
    return {
        "name": name,
        "browser_download_url": (
            f"https://github.com/ductv22/ducai-studio-releases/releases/download/v{version}/{name}"
        ),
        "size": len(data),
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }


def release(version, *, draft=False, published=True):
    return {
        "tag_name": f"v{version}",
        "draft": draft,
        "published_at": "2026-09-30T00:00:00Z" if published else None,
        "prerelease": "a" in version,
        "body": "Ghi chú phát hành an toàn.",
        "assets": [
            asset(expected_archive_name(version), version),
            asset("update-manifest.json", version),
            asset("update-manifest.sig", version),
        ],
    }


class Response(io.BytesIO):
    def __init__(self, body, url, status=200):
        super().__init__(body)
        self.url = url
        self.status = status

    def geturl(self):
        return self.url


def test_selects_highest_pep440_version_and_filters_unpublished():
    raw = [release("2.0.0a119"), release("2.0.0"), release("2.0.0a118")]
    raw += [release("2.0.1", draft=True), release("2.0.2", published=False)]
    parsed = [item for item in map(parse_release, raw) if item]
    assert select_release(parsed, "2.0.0a117", "testing").version == "2.0.0"
    assert select_release(parsed, "2.0.0a117", "stable").version == "2.0.0"
    assert select_release(parsed, "2.0.0", "stable") is None


def test_stable_excludes_release_marked_prerelease_even_with_final_version():
    raw = release("2.0.0")
    raw["prerelease"] = True
    item = parse_release(raw)
    assert select_release([item], "2.0.0a117", "stable") is None
    assert select_release([item], "2.0.0a117", "testing") is item
    assert item.notes == "Ghi chú phát hành an toàn."


def test_rejects_bad_hosts_and_duplicate_assets():
    for url in (
        "http://github.com/ductv22/ducai-studio-releases/releases/download/v1/x",
        "https://evil.example/x",
        "https://github.com/other/repo/releases/download/v1/x",
        "https://github.com:444/ductv22/ducai-studio-releases/releases/download/v1/x",
    ):
        with pytest.raises(UpdateCheckError):
            validate_https_url(url, asset=True)
    raw = release("2.0.0a118")
    raw["assets"].append(raw["assets"][0])
    with pytest.raises(UpdateCheckError, match="Duplicate"):
        parse_release(raw)


def test_client_verifies_signed_manifest_and_api_crosschecks():
    version = "2.0.0a118"
    archive = b"data"
    payload = {
        "schema": 1,
        "product": "DUCAI STUDIO",
        "version": version,
        "channel": "testing",
        "platform": "win64",
        "archive": {
            "name": expected_archive_name(version),
            "size": len(archive),
            "sha256": hashlib.sha256(archive).hexdigest(),
        },
        "files": [
            {"path": "DucaiStudio2.exe", "size": 3, "sha256": hashlib.sha256(b"exe").hexdigest()}
        ],
    }
    manifest = canonical_manifest_bytes(payload)
    key = Ed25519PrivateKey.generate()
    signature = key.sign(manifest)
    raw = release(version)
    raw["assets"] = [
        asset(expected_archive_name(version), version, archive),
        asset("update-manifest.json", version, manifest),
        asset("update-manifest.sig", version, signature),
    ]
    bodies = {
        "releases?": json.dumps([raw]).encode(),
        "update-manifest.json": manifest,
        "update-manifest.sig": signature,
    }

    def opener(request, timeout):
        url = request.full_url
        body = next(value for suffix, value in bodies.items() if suffix in url)
        return Response(body, url)

    client = GitHubUpdateClient(opener=opener)
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    checked = client.check("2.0.0a117", "testing", public_key)
    assert checked.manifest.version == version
    bad = release(version)
    bad["assets"][0]["size"] = 500
    bodies["releases?"] = json.dumps([bad]).encode()
    with pytest.raises(UpdateCheckError):
        client.check("2.0.0a117", "testing", public_key)


def test_client_rejects_unsafe_final_redirect_and_non_list_api():
    def unsafe(request, timeout):
        return Response(b"[]", "https://evil.example/release")

    with pytest.raises(UpdateCheckError):
        GitHubUpdateClient(opener=unsafe).list_releases()

    def malformed(request, timeout):
        return Response(b'{"error":"rate limited"}', request.full_url)

    with pytest.raises(UpdateCheckError):
        GitHubUpdateClient(opener=malformed).list_releases()
