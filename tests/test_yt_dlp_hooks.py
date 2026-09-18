"""Tests for reading a final file path out of yt-dlp's postprocessing hook.

A streamed (``-su``) upload is handed a path that has to exist at the moment it
is sent, and yt-dlp only reports that path in one place: the hook it fires when
``MoveFiles`` has finished. The payload around it is what makes this easy to get
wrong -- the info dict is a copy taken *before* the postprocessor ran, so its
``filepath`` is the path the file no longer has, and the move is recorded in the
nested ``__files_to_move`` mapping that both dicts share.
"""

from __future__ import annotations

from bot.helper.download.yt_dlp_hooks import final_paths_hook


def _moved(before, after, pp="MoveFiles", status="finished"):
    """A hook payload shaped the way yt-dlp builds one."""
    return {
        "status": status,
        "postprocessor": pp,
        "info_dict": {
            "filepath": before,
            "__files_to_move": {before: after},
        },
    }


def test_the_path_the_file_moved_to_is_the_one_reported():
    paths = []
    hook = final_paths_hook(paths.append)

    hook(_moved("/dl/clip.f137.mp4", "/dl/clip.mp4"))

    assert paths == ["/dl/clip.mp4"]


def test_a_file_that_did_not_have_to_move_is_reported_where_it_is():
    """Nothing recorded means the move was a no-op, so the copy's path stands."""
    paths = []
    hook = final_paths_hook(paths.append)

    hook({"status": "finished", "postprocessor": "MoveFiles", "info_dict": {"filepath": "/dl/clip.mp4"}})  # noqa: E501

    assert paths == ["/dl/clip.mp4"]


def test_a_started_postprocessor_has_no_file_yet():
    paths = []
    hook = final_paths_hook(paths.append)

    hook(_moved("/dl/clip.f137.mp4", "/dl/clip.mp4", status="started"))

    assert paths == []


def test_a_postprocessor_that_is_not_the_move_says_nothing():
    """Every postprocessor fires this hook; only one of them knows the path."""
    paths = []
    hook = final_paths_hook(paths.append)

    hook(_moved("/dl/clip.f137.mp4", "/dl/clip.mp4", pp="FFmpegMerger"))

    assert paths == []


def test_every_video_of_a_playlist_is_reported_in_order():
    paths = []
    hook = final_paths_hook(paths.append)

    for n in range(3):
        hook(_moved(f"/dl/{n}.f137.mp4", f"/dl/{n}.mp4"))

    assert paths == ["/dl/0.mp4", "/dl/1.mp4", "/dl/2.mp4"]


def test_a_file_reported_twice_is_listed_once():
    """Two postprocessors can both report the same output."""
    paths = []
    hook = final_paths_hook(paths.append)

    hook(_moved("/dl/clip.f137.mp4", "/dl/clip.mp4"))
    hook(_moved("/dl/clip.f137.mp4", "/dl/clip.mp4"))

    assert paths == ["/dl/clip.mp4"]
