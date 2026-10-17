"""GoFile direct link handler via gateway API."""

from __future__ import annotations

from os import path as ospath

from requests import Session

from .._common import (
    LOGGER,
    PASSWORD_ERROR_MESSAGE,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    user_agent,
)
from ..registry import register

_API_TIMEOUT = 30


def _build_header(download_info: dict[str, object]) -> list[str]:
    """The CDN request header as a list of 'Key: value' lines."""
    cookie = str(download_info.get("cookie") or "").strip()
    if not cookie:
        name = str(download_info.get("cookie_name") or "").strip()
        value = str(download_info.get("cookie_value") or "").strip()
        if name and value:
            cookie = f"{name}={value}"

    ua = str(download_info.get("user_agent") or "").strip() or user_agent
    header = [f"User-Agent: {ua}"]
    if cookie:
        header.insert(0, f"Cookie: {cookie}")
    return header


def _parse_files(
    files: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    """Extract valid file entries and total size from gateway files list."""
    contents: list[dict[str, object]] = []
    total_size = 0

    for file in files:
        dl_url = file.get("download_url") or file.get("link")
        if not dl_url or not isinstance(dl_url, str):
            continue

        raw_path = str(file.get("path") or "").lstrip("/")
        folder_path = ospath.dirname(raw_path).lstrip("/")
        filename = str(
            file.get("name")
            or ospath.basename(raw_path)
            or "unknown"
        )
        raw_size = file.get("size")
        if isinstance(raw_size, (int, float, str)):
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                size = 0
        else:
            size = 0

        total_size += size
        contents.append({
            "path": folder_path,
            "filename": filename,
            "url": dl_url,
        })

    return contents, total_size


def _fetch_from_gateway(url: str, password: str = "") -> dict[str, object]:
    """Call gateway gofile scrape endpoint and validate response."""
    api_url = gateway_url("/api/v1/scrape/gofile")
    params: dict[str, str] = {"q": url}
    if password:
        params["password"] = password

    with Session() as session:
        try:
            resp = session.get(
                api_url,
                params=params,
                headers=gateway_headers(),
                timeout=_API_TIMEOUT,
            )
            data = resp.json()
        except Exception as exc:
            raise DirectDownloadLinkException(
                f"ERROR: {exc.__class__.__name__}"
            ) from exc

    if not data.get("success"):
        err = str(data.get("error") or f"HTTP {resp.status_code}")
        err_lower = err.lower()
        if "password" in err_lower and (
            "required" in err_lower or "error-passwordrequired" in err_lower
        ):
            raise DirectDownloadLinkException(
                f"ERROR:\n{PASSWORD_ERROR_MESSAGE.format(url)}"
            )
        if "password" in err_lower and (
            "wrong" in err_lower or "error-passwordwrong" in err_lower
        ):
            raise DirectDownloadLinkException("ERROR: This password is wrong !")
        raise DirectDownloadLinkException(f"ERROR: {err}")

    return data


@register("gofile.io", order=20)
def gofile(url: str) -> tuple[str, list[str]] | dict[str, object]:
    """Resolve a GoFile link (single file or folder) via gateway API."""
    if "::" in url:
        clean_url, password = url.rsplit("::", 1)
        clean_url = clean_url.strip()
        password = password.strip()
    else:
        clean_url = url.strip()
        password = ""

    data = _fetch_from_gateway(clean_url, password)
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise DirectDownloadLinkException("ERROR: No files found in GoFile response")

    contents, total_size = _parse_files(files)
    if not contents:
        raise DirectDownloadLinkException("ERROR: No valid download links found")

    download_info = (
        data.get("download") if isinstance(data.get("download"), dict) else {}
    )
    header = _build_header(download_info)
    title = str(data.get("title") or contents[0]["filename"])

    LOGGER.info(f"GoFile: resolved {len(contents)} file(s) for '{title}'")

    if len(contents) == 1:
        return str(contents[0]["url"]), header

    details: dict[str, object] = {
        "contents": contents,
        "title": title,
        "total_size": total_size,
    }
    if header:
        details["header"] = "\n".join(header)
    return details
