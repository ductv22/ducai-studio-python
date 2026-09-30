"""Anonymous, bounded discovery of signed GitHub update releases."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from packaging.version import InvalidVersion, Version

from ducai_studio.infrastructure.update_manifest import (
    MAX_MANIFEST_BYTES,
    PUBLIC_KEY_BYTES,
    UpdateManifest,
    UpdateManifestError,
    expected_archive_name,
    verify_manifest,
    version_is_eligible,
)

OWNER = "ductv22"
REPOSITORY = "ducai-studio-releases"
API_ROOT = f"https://api.github.com/repos/{OWNER}/{REPOSITORY}/releases"
MAX_RELEASE_PAGES = 5
MAX_RELEASE_JSON_BYTES = 2 * 1024 * 1024
MAX_SIGNATURE_BYTES = 128
MAX_RELEASE_NOTES_CHARS = 4_096
_ASSET_NAMES = ("update-manifest.json", "update-manifest.sig")
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class UpdateCheckError(RuntimeError):
    """A release check failed; this is never equivalent to no update."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubRelease:
    version: str
    tag: str
    prerelease: bool
    notes: str
    archive: ReleaseAsset
    manifest: ReleaseAsset
    signature: ReleaseAsset


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    release: GitHubRelease
    manifest: UpdateManifest
    manifest_bytes: bytes
    signature_bytes: bytes


def validate_https_url(url: str, *, asset: bool) -> str:
    """Restrict initial URLs and every redirect to GitHub's update endpoints."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UpdateCheckError("Invalid update URL") from exc
    if (
        parsed.scheme != "https"
        or not host
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UpdateCheckError("Update URL must use authenticated HTTPS")
    if not asset:
        release_path = f"/repos/{OWNER}/{REPOSITORY}/releases"
        if host != "api.github.com" or not (
            parsed.path == release_path or parsed.path.startswith(release_path + "/")
        ):
            raise UpdateCheckError("Unexpected GitHub API URL")
    elif host == "github.com":
        if not parsed.path.startswith(f"/{OWNER}/{REPOSITORY}/releases/download/"):
            raise UpdateCheckError("Unexpected GitHub release asset URL")
    elif host not in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        raise UpdateCheckError("Unexpected release download host")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, asset: bool) -> None:
        self.asset = asset

    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request:
        validate_https_url(newurl, asset=self.asset)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _open(url: str, *, asset: bool, timeout: float, opener: Callable[..., Any] | None) -> Any:
    validate_https_url(url, asset=asset)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream" if asset else "application/vnd.github+json",
            "User-Agent": "DUCAI-STUDIO-updater",
        },
    )
    real_opener = opener or urllib.request.build_opener(_SafeRedirectHandler(asset=asset)).open
    try:
        response = real_opener(request, timeout=timeout)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateCheckError(f"GitHub update request failed: {exc}") from exc
    try:
        final_url = response.geturl()
        validate_https_url(final_url, asset=asset)
        status = getattr(response, "status", 200)
        if status in _REDIRECT_CODES or status != 200:
            raise UpdateCheckError(f"Unexpected GitHub HTTP status: {status}")
    except Exception:
        response.close()
        raise
    return response


def open_update_url(
    url: str, *, asset: bool = True, timeout: float = 30.0, opener: Callable[..., Any] | None = None
) -> Any:
    """Open a verified-host endpoint; caller owns the returned response."""
    return _open(url, asset=asset, timeout=timeout, opener=opener)


def _read_bounded(response: BinaryIO, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise UpdateCheckError("GitHub response exceeds size limit")
    return body


def _asset(raw: Any, name: str, tag: str) -> ReleaseAsset:
    if not isinstance(raw, dict) or raw.get("name") != name:
        raise UpdateCheckError("Malformed release asset")
    url = raw.get("browser_download_url")
    if not isinstance(url, str):
        raise UpdateCheckError("Release asset lacks download URL")
    validate_https_url(url, asset=True)
    expected_path = f"/{OWNER}/{REPOSITORY}/releases/download/{tag}/{name}"
    if urllib.parse.urlsplit(url).path != expected_path:
        raise UpdateCheckError("Release asset URL does not match its tag and name")
    size = raw.get("size")
    if type(size) is not int or size < 1:
        raise UpdateCheckError("Release asset has invalid size")
    digest = raw.get("digest")
    if digest is not None:
        if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
            raise UpdateCheckError("Malformed release asset digest")
        sha256 = digest[7:].lower()
        if any(char not in "0123456789abcdef" for char in sha256):
            raise UpdateCheckError("Malformed release asset digest")
    else:
        sha256 = None
    return ReleaseAsset(name, url, size, sha256)


def parse_release(raw: Any) -> GitHubRelease | None:
    """Ignore drafts/unpublished releases; reject a malformed eligible release."""
    if not isinstance(raw, dict):
        raise UpdateCheckError("Malformed GitHub release")
    published_at = raw.get("published_at")
    if raw.get("draft") is not False or not isinstance(published_at, str) or not published_at:
        return None
    tag = raw.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        return None
    version = tag[1:]
    try:
        parsed_version = Version(version)
    except InvalidVersion:
        return None
    if (
        str(parsed_version) != version
        or parsed_version.local is not None
        or parsed_version.is_devrelease
    ):
        return None
    prerelease = raw.get("prerelease")
    if type(prerelease) is not bool:
        raise UpdateCheckError("Release prerelease flag is invalid")
    raw_notes = raw.get("body")
    if raw_notes is not None and not isinstance(raw_notes, str):
        raise UpdateCheckError("Release notes are invalid")
    notes = (raw_notes or "")[:MAX_RELEASE_NOTES_CHARS]
    raw_assets = raw.get("assets")
    if not isinstance(raw_assets, list):
        raise UpdateCheckError("Release assets are missing")
    wanted = {expected_archive_name(version), *_ASSET_NAMES}
    matches: dict[str, Any] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise UpdateCheckError("Malformed release asset")
        name = raw_asset.get("name")
        if name in wanted:
            if name in matches:
                raise UpdateCheckError(f"Duplicate release asset: {name}")
            matches[name] = raw_asset
    if matches.keys() != wanted:
        raise UpdateCheckError("Required update assets are missing")
    return GitHubRelease(
        version,
        tag,
        prerelease,
        notes,
        _asset(matches[expected_archive_name(version)], expected_archive_name(version), tag),
        _asset(matches[_ASSET_NAMES[0]], _ASSET_NAMES[0], tag),
        _asset(matches[_ASSET_NAMES[1]], _ASSET_NAMES[1], tag),
    )


def select_release(
    releases: list[GitHubRelease], current_version: str, channel: str
) -> GitHubRelease | None:
    eligible = [
        item
        for item in releases
        if version_is_eligible(item.version, current_version, channel)
        and (channel == "testing" or not item.prerelease)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: Version(item.version))


class GitHubUpdateClient:
    def __init__(self, *, timeout: float = 30.0, opener: Callable[..., Any] | None = None) -> None:
        self.timeout = timeout
        self.opener = opener

    def list_releases(self) -> list[GitHubRelease]:
        releases: list[GitHubRelease] = []
        seen_versions: set[str] = set()
        for page in range(1, MAX_RELEASE_PAGES + 1):
            url = f"{API_ROOT}?per_page=100&page={page}"
            with _open(url, asset=False, timeout=self.timeout, opener=self.opener) as response:
                try:
                    page_data = json.loads(_read_bounded(response, MAX_RELEASE_JSON_BYTES))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise UpdateCheckError("Invalid GitHub release JSON") from exc
            if not isinstance(page_data, list):
                raise UpdateCheckError("Invalid GitHub release list")
            for raw in page_data:
                item = parse_release(raw)
                if item is not None:
                    if item.version in seen_versions:
                        raise UpdateCheckError("Conflicting releases for one version")
                    seen_versions.add(item.version)
                    releases.append(item)
            if len(page_data) < 100:
                break
            if page == MAX_RELEASE_PAGES:
                raise UpdateCheckError("GitHub release pagination limit reached")
        return releases

    def check(
        self, current_version: str, channel: str, public_key: bytes = PUBLIC_KEY_BYTES
    ) -> VerifiedRelease | None:
        release = select_release(self.list_releases(), current_version, channel)
        if release is None:
            return None
        if release.manifest.size > MAX_MANIFEST_BYTES or release.signature.size != 64:
            raise UpdateCheckError("Signed metadata asset size is invalid")
        with _open(
            release.manifest.url, asset=True, timeout=self.timeout, opener=self.opener
        ) as response:
            manifest_bytes = _read_bounded(response, MAX_MANIFEST_BYTES)
        with _open(
            release.signature.url, asset=True, timeout=self.timeout, opener=self.opener
        ) as response:
            signature = _read_bounded(response, MAX_SIGNATURE_BYTES)
        if len(manifest_bytes) != release.manifest.size or len(signature) != release.signature.size:
            raise UpdateCheckError("Signed metadata asset size mismatch")
        try:
            manifest = verify_manifest(manifest_bytes, signature, public_key)
        except UpdateManifestError as exc:
            raise UpdateCheckError(f"Untrusted update manifest: {exc}") from exc
        if manifest.version != release.version or manifest.archive.name != release.archive.name:
            raise UpdateCheckError("Manifest does not match GitHub release")
        if channel == "stable" and manifest.channel != "stable":
            raise UpdateCheckError("Stable channel requires a stable signed manifest")
        if manifest.archive.size != release.archive.size:
            raise UpdateCheckError("GitHub archive size conflicts with signed manifest")
        if release.archive.sha256 and manifest.archive.sha256 != release.archive.sha256:
            raise UpdateCheckError("GitHub archive digest conflicts with signed manifest")
        if (
            release.manifest.sha256
            and hashlib.sha256(manifest_bytes).hexdigest() != release.manifest.sha256
        ):
            raise UpdateCheckError("GitHub manifest digest mismatch")
        if (
            release.signature.sha256
            and hashlib.sha256(signature).hexdigest() != release.signature.sha256
        ):
            raise UpdateCheckError("GitHub signature digest mismatch")
        return VerifiedRelease(release, manifest, manifest_bytes, signature)
