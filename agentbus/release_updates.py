"""Check the publisher's public GitHub Releases channel without sending project data."""
from __future__ import annotations

import http.client
import json
import re
import ssl
import threading
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__, gui_state


# Set this in the publisher's source before tagging the first public build.
# A project, renderer, or remote response cannot select a different repository.
GITHUB_RELEASE_REPOSITORY = "wuerchenai-cell/AgentBus"
WINDOWS_ASSET = "AgentBus-win-x64-green.zip"
_MAX_RESPONSE_BYTES = 512 * 1024
_NETWORK_TIMEOUT_S = 5
_AUTOMATIC_CHECK_INTERVAL_S = 24 * 60 * 60
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", re.ASCII)
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})", re.ASCII)


class _MetadataTooLarge(ValueError):
    """The public response exceeded the accepted metadata size."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_repository(value: str) -> bool:
    return (
        isinstance(value, str) and bool(_REPOSITORY.fullmatch(value))
        and value.split("/")[-1] not in {".", ".."}
        and not value.casefold().endswith(".git")
    )


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _fetch_releases(repository: str) -> list:
    request = Request(
        f"https://api.github.com/repos/{repository}/releases?per_page=20",
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2026-03-10",
                 "User-Agent": "AgentBus-release-check"},
    )
    started = time.monotonic()
    with build_opener(_NoRedirect()).open(request, timeout=_NETWORK_TIMEOUT_S) as response:
        data = bytearray()
        while len(data) <= _MAX_RESPONSE_BYTES:
            if time.monotonic() - started > _NETWORK_TIMEOUT_S:
                raise TimeoutError("release check exceeded its read budget")
            chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise _MetadataTooLarge("oversized release response")
    return json.loads(data.decode("utf-8"))


def check_for_update(repository: str, current_version: str, *, fetch=None) -> dict:
    result = {"current_version": current_version}
    if not configured_repository(repository):
        return {**result, "status": "not_configured"}
    local = _version_tuple(current_version)
    if local is None:
        return {**result, "status": "unavailable"}
    try:
        releases = (fetch or _fetch_releases)(repository)
    except HTTPError as exc:
        exc.close()
        return {**result, "status": "no_release" if exc.code == 404 else "unavailable"}
    except (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException,
            UnicodeDecodeError, json.JSONDecodeError, RecursionError, _MetadataTooLarge):
        return {**result, "status": "unavailable"}
    if not isinstance(releases, list):
        return {**result, "status": "unavailable"}

    stable_found = False
    windows_versions = []
    for release in releases:
        if (not isinstance(release, dict) or release.get("draft") is not False
                or release.get("prerelease") is not False):
            continue
        tag = release.get("tag_name")
        remote = _version_tuple(tag[1:]) if isinstance(tag, str) and tag.startswith("v") else None
        if remote is None:
            continue
        stable_found = True
        assets = release.get("assets")
        if isinstance(assets, list) and any(
            isinstance(asset, dict) and asset.get("name") == WINDOWS_ASSET
            and asset.get("state") == "uploaded" and type(asset.get("size")) is int and asset["size"] > 0
            for asset in assets
        ):
            windows_versions.append(remote)
    if not windows_versions:
        return {**result, "status": "no_asset" if stable_found else "no_release"}
    newest = max(windows_versions)
    if newest <= local:
        return {**result, "status": "current"}
    return {**result, "status": "update_available", "version": ".".join(map(str, newest))}


class ReleaseUpdates:
    """Local desktop controls. Network work is independent of the project bridge lock."""

    def __init__(self):
        self._check_lock = threading.Lock()
        self._release_page = None
        self._next_automatic_check = None

    def preferences(self) -> dict:
        configured = configured_repository(GITHUB_RELEASE_REPOSITORY)
        return {"current_version": __version__, "configured": configured,
                "automatic": configured and gui_state.automatic_release_checks()}

    def set_preferences(self, automatic: bool) -> dict:
        if type(automatic) is not bool:
            raise ValueError("自动检查更新设置必须为开或关。")
        if automatic and not configured_repository(GITHUB_RELEASE_REPOSITORY):
            raise ValueError("此构建尚未绑定发布仓库。")
        if not gui_state.save_automatic_release_checks(automatic):
            raise ValueError("保存更新偏好失败，请稍后重试。")
        return self.preferences()

    def check(self, automatic: bool = False) -> dict:
        if type(automatic) is not bool:
            raise ValueError("自动检查标记必须为布尔值。")
        repository = GITHUB_RELEASE_REPOSITORY
        if not configured_repository(repository):
            return {"current_version": __version__, "status": "not_configured"}
        if not self._check_lock.acquire(blocking=False):
            return {"current_version": __version__, "status": "deferred"}
        try:
            if automatic:
                if not gui_state.automatic_release_checks():
                    return {"current_version": __version__, "status": "disabled"}
                now = time.monotonic()
                if self._next_automatic_check is not None and now < self._next_automatic_check:
                    return {"current_version": __version__, "status": "deferred"}
                self._next_automatic_check = now + _AUTOMATIC_CHECK_INTERVAL_S
            result = check_for_update(repository, __version__)
            if result["status"] == "update_available":
                self._release_page = f"https://github.com/{repository}/releases/tag/v{result['version']}"
            elif result["status"] in {"current", "no_release", "no_asset"}:
                self._release_page = None
            return result
        finally:
            self._check_lock.release()

    def open_release_page(self) -> dict:
        url = self._release_page
        if not url:
            raise ValueError("请先检查并确认有可用的新版本。")
        if not webbrowser.open(url):
            raise ValueError("无法打开浏览器。")
        return {"opened": True}
