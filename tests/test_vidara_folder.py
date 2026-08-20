"""Tests for the Vidara folder listing.

``/f/<code>`` is a folder page, and the stream API only takes a file code, so
every folder link a bulk carried came back as ``no file code found in "/f/…"``
and was counted as a dead link. These tests pin that a folder link is expanded
into its videos instead: one entry per video, named uniquely, subfolders walked,
and the caps that bound the walk visible rather than silent.

The ``vidara`` fixture (conftest.py) loads the host module with its package
stubbed out.
"""

from __future__ import annotations

import pytest


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _FolderSession:
    """Answers a folder listing per ``q``, so a walk can be scripted.

    ``pages`` maps the code (or URL) the walker asks for to a response; anything
    unmapped answers 404, which is what a folder that no longer exists looks
    like.
    """

    def __init__(self, pages):
        self._pages = pages
        self.asked = []

    def get(self, url, params=None, **kwargs):
        target = (params or {}).get("q", "")
        self.asked.append(target)
        for key, response in self._pages.items():
            if key in target:
                if isinstance(response, list):
                    # a scripted sequence: the last answer repeats
                    return response.pop(0) if len(response) > 1 else response[0]
                return response
        return _Resp(404, {"success": False, "error": "folder not found"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _video(code, title, duration=42):
    return {
        "file_code": code,
        "title": title,
        "filename": f"{title}.mp4",
        "duration_seconds": duration,
        "page_url": f"https://vidara.to/v/{code}",
        "embed_url": f"https://vidara.to/e/{code}",
    }


def _folder(code, name, videos=(), subfolders=()):
    return _Resp(
        200,
        {
            "success": True,
            "folder_code": code,
            "folder_url": f"https://vidara.to/f/{code}",
            "host": "vidara.to",
            "name": name,
            "video_count": len(videos),
            "subfolder_count": len(subfolders),
            "streams_resolved": False,
            "videos": list(videos),
            "subfolders": list(subfolders),
        },
    )


def _subfolder(code, name):
    return {"code": code, "name": name, "url": f"https://vidara.to/f/{code}"}


def _use(vidara, monkeypatch, pages):
    session = _FolderSession(pages)
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)
    return session


# ── recognising a folder link ────────────────────────────────────────


@pytest.mark.parametrize(
    "url, code",
    [
        ("https://vidara.to/f/WIhdXPC", "WIhdXPC"),
        ("https://vidara.so/f/WIhdXPC/", "WIhdXPC"),
        ("https://vidara.to/F/WIhdXPC", "WIhdXPC"),
        ("https://vidara.to/e/m001L0vUgomWJ", ""),
        ("https://vidara.to/v/m001L0vUgomWJ", ""),
        ("https://vidara.to/f/", ""),
        ("https://vidara.to/", ""),
        (None, ""),
    ],
)
def test_folder_code_is_read_off_the_path(vidara, url, code):
    assert vidara.vidara_folder_code(url) == code


def test_folder_predicate_needs_both_a_vidara_host_and_an_f_path(vidara):
    assert vidara.is_vidara_folder_link("https://vidara.to/f/WIhdXPC")
    assert not vidara.is_vidara_folder_link("https://vidara.to/e/abc")
    # a lookalike host with the same path shape is not Vidara
    assert not vidara.is_vidara_folder_link("https://notvidara.com/f/WIhdXPC")


# ── the listing ──────────────────────────────────────────────────────


def test_handler_expands_a_folder_into_its_videos(vidara, monkeypatch):
    _use(
        vidara,
        monkeypatch,
        {"WIhdXPC": _folder("WIhdXPC", "ZILVIAZU", [_video("aaa", "clip 1"),
                                                    _video("bbb", "clip 2")])},
    )

    resolved = vidara.vidara("https://vidara.to/f/WIhdXPC")

    assert resolved["vidara"] is True
    assert resolved["title"] == "ZILVIAZU"
    assert resolved["folder_url"] == "https://vidara.to/f/WIhdXPC"
    assert [entry["name"] for entry in resolved["videos"]] == ["clip 1", "clip 2"]
    assert [entry["url"] for entry in resolved["videos"]] == [
        "https://vidara.to/v/aaa",
        "https://vidara.to/v/bbb",
    ]
    assert all(entry["subpath"] == "" for entry in resolved["videos"])


def test_entry_names_carry_no_extension(vidara, monkeypatch):
    """yt-dlp appends the container it muxed into, so a title handed over whole
    lands as "clip.mp4.mp4"."""
    _use(vidara, monkeypatch,
         {"WIhdXPC": _folder("WIhdXPC", "F", [_video("aaa", "clip")])})

    entry = vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")["videos"][0]

    assert entry["name"] == "clip"


def test_repeated_titles_do_not_overwrite_each_other(vidara, monkeypatch):
    _use(
        vidara,
        monkeypatch,
        {"WIhdXPC": _folder("WIhdXPC", "F", [_video("aaa", "clip"),
                                             _video("bbb", "clip")])},
    )

    names = [
        entry["name"]
        for entry in vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")["videos"]
    ]

    assert names == ["clip", "clip bbb"]
    assert len(set(names)) == 2


def test_a_title_cannot_decide_where_the_file_lands(vidara, monkeypatch):
    video = _video("aaa", "clip")
    video["filename"] = "../../etc/passwd.mp4"
    _use(vidara, monkeypatch, {"WIhdXPC": _folder("WIhdXPC", "../root", [video])})

    resolved = vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")

    assert resolved["videos"][0]["name"] == "passwd"
    assert "/" not in resolved["title"]


def test_an_entry_with_no_file_code_is_skipped(vidara, monkeypatch):
    _use(
        vidara,
        monkeypatch,
        {
            "WIhdXPC": _folder(
                "WIhdXPC", "F",
                [{"title": "ghost"}, _video("aaa", "clip")],
            )
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")["videos"]

    assert [entry["name"] for entry in videos] == ["clip"]


def test_an_empty_folder_is_reported_not_started(vidara, monkeypatch):
    _use(vidara, monkeypatch, {"WIhdXPC": _folder("WIhdXPC", "F", [])})

    with pytest.raises(vidara.DirectDownloadLinkException, match="no videos"):
        vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")


# ── subfolders ───────────────────────────────────────────────────────


def test_subfolders_are_walked_and_keep_their_own_directory(vidara, monkeypatch):
    _use(
        vidara,
        monkeypatch,
        {
            "ROOT": _folder("ROOT", "Show", [_video("aaa", "trailer")],
                            [_subfolder("S1", "Season 1")]),
            "S1": _folder("S1", "Season 1", [_video("bbb", "ep1")]),
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/ROOT")["videos"]

    assert [(v["name"], v["subpath"]) for v in videos] == [
        ("trailer", ""),
        ("ep1", "Season 1"),
    ]


def test_nested_subfolders_nest_their_paths(vidara, monkeypatch):
    _use(
        vidara,
        monkeypatch,
        {
            "ROOT": _folder("ROOT", "Show", [], [_subfolder("S1", "Season 1")]),
            "S1": _folder("S1", "Season 1", [], [_subfolder("D1", "Disc 1")]),
            "D1": _folder("D1", "Disc 1", [_video("aaa", "ep1")]),
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/ROOT")["videos"]

    assert videos[0]["subpath"] == "Season 1/Disc 1"


def test_a_subfolder_pointing_back_at_its_parent_does_not_loop(vidara, monkeypatch):
    session = _use(
        vidara,
        monkeypatch,
        {
            "ROOT": _folder("ROOT", "Show", [_video("aaa", "clip")],
                            [_subfolder("ROOT", "itself"),
                             _subfolder("S1", "Season 1")]),
            "S1": _folder("S1", "Season 1", [_video("bbb", "ep1")],
                          [_subfolder("ROOT", "back up")]),
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/ROOT")["videos"]

    assert len(videos) == 2
    assert session.asked.count("https://vidara.to/f/ROOT") == 1


def test_a_dead_subfolder_costs_its_own_videos_only(vidara, monkeypatch):
    session = _use(
        vidara,
        monkeypatch,
        {
            "ROOT": _folder("ROOT", "Show", [_video("aaa", "clip")],
                            [_subfolder("GONE", "Season 1")]),
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/ROOT")["videos"]

    assert [entry["name"] for entry in videos] == ["clip"]
    # the 404 for the subfolder is final, so it is asked for exactly once
    assert session.asked.count("https://vidara.to/f/GONE") == 1


def test_the_requested_folder_failing_fails_the_task(vidara, monkeypatch):
    _use(vidara, monkeypatch, {})

    with pytest.raises(vidara.DirectDownloadLinkException, match="folder not found"):
        vidara.vidara_folder_list("https://vidara.to/f/ROOT")


def test_depth_cap_stops_the_walk(vidara, monkeypatch):
    monkeypatch.setattr(vidara, "VIDARA_FOLDER_MAX_DEPTH", 1)
    _use(
        vidara,
        monkeypatch,
        {
            "ROOT": _folder("ROOT", "Show", [], [_subfolder("S1", "Season 1")]),
            "S1": _folder("S1", "Season 1", [_video("aaa", "ep1")],
                          [_subfolder("D1", "Disc 1")]),
            "D1": _folder("D1", "Disc 1", [_video("bbb", "ep2")]),
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/ROOT")["videos"]

    assert [entry["name"] for entry in videos] == ["ep1"]


def test_video_cap_trims_the_listing(vidara, monkeypatch):
    monkeypatch.setattr(vidara, "VIDARA_FOLDER_MAX_VIDEOS", 2)
    _use(
        vidara,
        monkeypatch,
        {
            "ROOT": _folder(
                "ROOT", "Show",
                [_video("aaa", "c1"), _video("bbb", "c2"), _video("ccc", "c3")],
                [_subfolder("S1", "Season 1")],
            ),
            "S1": _folder("S1", "Season 1", [_video("ddd", "ep1")]),
        },
    )

    videos = vidara.vidara_folder_list("https://vidara.to/f/ROOT")["videos"]

    assert [entry["name"] for entry in videos] == ["c1", "c2"]


# ── retrying the listing ─────────────────────────────────────────────


def test_a_rate_limited_listing_is_retried(vidara, monkeypatch):
    session = _use(
        vidara,
        monkeypatch,
        {
            "WIhdXPC": [
                _Resp(429, {"success": False, "error": "slow down"},
                      {"Retry-After": "2"}),
                _folder("WIhdXPC", "F", [_video("aaa", "clip")]),
            ]
        },
    )

    resolved = vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")

    assert len(resolved["videos"]) == 1
    assert len(session.asked) == 2


def test_a_rate_limit_wrapped_in_a_200_is_retried(vidara, monkeypatch):
    session = _use(
        vidara,
        monkeypatch,
        {
            "WIhdXPC": [
                _Resp(200, {"success": False, "error": "Rate limit hit"}),
                _folder("WIhdXPC", "F", [_video("aaa", "clip")]),
            ]
        },
    )

    assert len(vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")["videos"]) == 1
    assert len(session.asked) == 2


def test_a_removed_folder_is_not_retried(vidara, monkeypatch):
    session = _use(
        vidara,
        monkeypatch,
        {"WIhdXPC": _Resp(404, {"success": False, "error": "folder not found"})},
    )

    with pytest.raises(vidara.DirectDownloadLinkException, match="folder not found"):
        vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")

    assert len(session.asked) == 1


def test_a_rate_limit_that_never_lets_up_gives_up_after_its_attempts(
    vidara, monkeypatch
):
    session = _use(
        vidara,
        monkeypatch,
        {"WIhdXPC": _Resp(429, {"success": False, "error": "slow down"})},
    )

    with pytest.raises(vidara.DirectDownloadLinkException, match="slow down"):
        vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")

    assert len(session.asked) == vidara.VIDARA_ATTEMPTS


def test_the_listing_asks_for_no_streams(vidara, monkeypatch):
    """Resolving here would mint IP-bound URLs that expire while the videos
    ahead of them are still downloading."""
    seen = {}

    class _Session:
        def get(self, url, params=None, **kwargs):
            seen.update({"url": url, "params": params})
            return _folder("WIhdXPC", "F", [_video("aaa", "clip")])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(vidara, "Session", lambda: _Session())

    vidara.vidara_folder_list("https://vidara.to/f/WIhdXPC")

    assert seen["url"].endswith("/api/v1/scrape/vidara/folder")
    assert seen["params"]["resolve"] == "false"
