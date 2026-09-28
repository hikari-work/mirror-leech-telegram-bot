"""ShyAV and kpop.xxx hosts: what one link turns into.

Both answer with a listing -- episodes for a series, videos for a playlist --
and both mint signed CDN URLs that expire, so the handler returns page URLs and
asks ``DirectListener`` to resolve each one as its turn comes. These tests pin
that shape: the entries, the ``lazy`` name the resolver is looked up by, and
the single-video path that hands over the link it was already given.

The gateway is a scripted session, so no network is involved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent

class _Logger:
    @staticmethod
    def info(msg):
        pass

    error = warning = debug = info

class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

class _Session:
    """Answers a gateway read per the ``q`` / ``url`` it was asked for."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, **kwargs):
        params = params or {}
        key = params.get("q") or params.get("url") or ""
        for marker, payload in self._payload.items():
            if marker in url or marker in str(key):
                return _Resp(payload)
        return _Resp({"success": False, "error": "not found"}, 404)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

@pytest.fixture
def hosts(monkeypatch):
    """Load both host modules with the package stubbed out."""
    pkg = ModuleType("host_stub")
    pkg.__path__ = []
    hosts_pkg = ModuleType("host_stub.hosts")
    hosts_pkg.__path__ = []

    class DirectDownloadLinkException(Exception):
        pass

    common = ModuleType("host_stub._common")
    common.LOGGER = _Logger()
    common.DirectDownloadLinkException = DirectDownloadLinkException
    common.user_agent = "UA"
    common.gateway_url = lambda path="": f"https://gateway.test{path}"
    common.gateway_headers = lambda accept_json=True: {"accept": "application/json"}

    from os import path as ospath
    from re import sub as resub

    common.MEDIA_EXTS = frozenset((".mp4", ".mkv", ".webm", ".m4v"))

    def safe_name(name, fallback):
        cleaned = resub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip()).strip(" .")
        return cleaned[:200] or fallback

    def safe_stem(title, fallback):
        stem, ext = ospath.splitext(safe_name(title, fallback))
        return stem if ext.lower() in common.MEDIA_EXTS else safe_name(title, fallback)

    def unique_stem(stem, taken, code=""):
        if (key := stem.lower()) not in taken:
            taken.add(key)
            return stem
        stem = f"{stem} {code}" if code else f"{stem} {len(taken)}"
        taken.add(stem.lower())
        return stem

    common.safe_name = safe_name
    common.safe_stem = safe_stem
    common.unique_stem = unique_stem

    # the two host modules import sync_to_async from the real bot package
    util_pkg = ModuleType("host_stub.util")
    util_pkg.__path__ = []

    registry = ModuleType("host_stub.registry")
    registry.register = lambda **kwargs: (lambda func: func)

    for name, mod in {
        "host_stub": pkg,
        "host_stub.hosts": hosts_pkg,
        "host_stub._common": common,
        "host_stub.registry": registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    loaded = {}
    for name in ("shyav", "kpop"):
        path = (
            _ROOT / "bot" / "helper" / "download" / "direct_link_generators"
            / "hosts" / f"{name}.py"
        )
        spec = importlib.util.spec_from_file_location(f"host_stub.hosts.{name}", path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded

def _use(module, monkeypatch, payload):
    session = _Session(payload)
    monkeypatch.setattr(module, "Session", lambda: session, raising=False)
    return session

# ── shyav ────────────────────────────────────────────────────────────

def _shyav_detail(**overrides):
    data = {
        "success": True,
        "data": {
            "slug": "a-series",
            "title": "A Series",
            "page_url": "https://shyav.com/video.php?slug=a-series",
            "video_url": "https://cdn.test/1.mp4?verify=abc",
            "total_episodes": 2,
            "current_episode": 1,
            "episodes": [
                {
                    "episode": 1,
                    "fid": "111",
                    "label": "동영상 1",
                    "url": "https://shyav.com/video.php?slug=a-series&fid=111&ep=1",
                },
                {
                    "episode": 2,
                    "fid": "222",
                    "label": "동영상 2",
                    "url": "https://shyav.com/video.php?slug=a-series&fid=222&ep=2",
                },
            ],
        },
    }
    data["data"].update(overrides)
    return data

def test_shyav_matches_the_host_and_its_subdomains(hosts):
    is_shyav = hosts["shyav"].is_shyav
    assert is_shyav("https://shyav.com/video.php?slug=x")
    assert is_shyav("https://www.shyav.com/video.php?slug=x")
    # a lookalike is not the real thing
    assert not is_shyav("https://notshyav.com/video.php?slug=x")

def test_a_series_is_one_entry_per_episode(hosts, monkeypatch):
    """The listing is what the user's link stands for, so every episode is an
    entry -- and each one still points at its page, not at a signed URL."""
    module = hosts["shyav"]
    _use(module, monkeypatch, {"scrape/shyav": _shyav_detail()})

    result = module.shyav("https://shyav.com/video.php?slug=a-series")

    assert result["lazy"] == "shyav"
    assert result["title"] == "A Series"
    assert [c["filename"] for c in result["contents"]] == [
        "동영상 1.mp4",
        "동영상 2.mp4",
    ]
    assert result["contents"][1]["url"].endswith("fid=222")

def test_a_single_video_hands_over_the_link_it_was_given(hosts, monkeypatch):
    """Nothing to list, so the resolved MP4 travels with the result and no
    lazy name is set -- there is no second resolve to do."""
    module = hosts["shyav"]
    _use(
        module,
        monkeypatch,
        {
            "scrape/shyav": _shyav_detail(
                total_episodes=0,
                episodes=None,
                title="Lonely Clip",
                page_url="https://shyav.com/video.php?slug=lonely",
                video_url="https://cdn.test/lonely.mp4?verify=abc",
            )
        },
    )

    result = module.shyav("https://shyav.com/video.php?slug=lonely")

    assert "lazy" not in result
    assert result["contents"] == [
        {
            "path": "",
            "filename": "Lonely Clip.mp4",
            "url": "https://cdn.test/lonely.mp4?verify=abc",
        }
    ]

def test_shyav_reports_a_page_with_no_video(hosts, monkeypatch):
    """No episodes and no MP4 is a page with nothing to download, not a task
    that reports success over an empty listing."""
    module = hosts["shyav"]
    _use(
        module,
        monkeypatch,
        {"scrape/shyav": _shyav_detail(total_episodes=0, episodes=None, video_url="")},
    )

    with pytest.raises(Exception) as excinfo:
        module.shyav("https://shyav.com/video.php?slug=lonely")
    assert "no video" in str(excinfo.value)

def test_shyav_episode_fid_comes_off_the_query(hosts):
    episode_fid = hosts["shyav"].shyav_episode_fid
    assert episode_fid("https://shyav.com/video.php?slug=x&fid=37010&ep=346") == "37010"
    assert episode_fid("https://shyav.com/video.php?slug=x") == ""
    assert episode_fid(None) == ""

def test_an_episode_link_carries_one_fid(hosts):
    """The link the user sent may already name an episode, and appending a
    second fid leaves the gateway to pick one of the two."""
    episode_url = hosts["shyav"].shyav_episode_url
    url = episode_url("https://shyav.com/video.php?slug=x&fid=1&ep=1", "222")
    assert url.count("fid=") == 1
    assert "fid=222" in url
    assert "ep=1" in url

# ── kpop ─────────────────────────────────────────────────────────────

def test_kpop_matches_the_host_and_its_subdomains(hosts):
    is_kpop = hosts["kpop"].is_kpop
    assert is_kpop("https://kpop.xxx/video/760625/x/")
    assert is_kpop("https://www.kpop.xxx/video/760625/x/")
    assert not is_kpop("https://notkpop.xxx/video/760625/x/")

@pytest.mark.parametrize(
    ("url", "is_playlist"),
    [
        ("https://kpop.xxx/playlists/10346/6-inflw/", True),
        ("https://kpop.xxx/playlists/10346", True),
        ("https://kpop.xxx/video/760625/x/", False),
        ("https://kpop.xxx/", False),
        (None, False),
    ],
)
def test_kpop_playlist_links_are_recognised_by_their_path(hosts, url, is_playlist):
    assert hosts["kpop"].is_kpop_playlist(url) is is_playlist

def _kpop_playlist(videos=2):
    return {
        "success": True,
        "data": {
            "title": "1.판매꼴",
            "page_url": "https://kpop.xxx/playlists/10346/6-inflw/",
            "total_videos": videos,
            "total_pages": 19,
            "videos": [
                {
                    "video_id": str(100 + i),
                    "title": f"clip {i}",
                    "url": f"https://kpop.xxx/video/{100 + i}/clip-{i}/",
                }
                for i in range(videos)
            ],
        },
    }

def test_a_playlist_is_one_entry_per_video(hosts, monkeypatch):
    module = hosts["kpop"]
    _use(module, monkeypatch, {"kpop/playlist": _kpop_playlist()})

    result = module.kpop("https://kpop.xxx/playlists/10346/6-inflw/")

    assert result["lazy"] == "kpop"
    assert result["title"] == "1.판매꼴"
    assert [c["filename"] for c in result["contents"]] == ["clip 0.mp4", "clip 1.mp4"]
    assert result["contents"][0]["url"] == "https://kpop.xxx/video/100/clip-0/"

def test_videos_that_share_a_title_keep_a_file_each(hosts, monkeypatch):
    """The second copy would otherwise land on top of the first."""
    module = hosts["kpop"]
    payload = _kpop_playlist()
    for video in payload["data"]["videos"]:
        video["title"] = "same name"
    _use(module, monkeypatch, {"kpop/playlist": payload})

    result = module.kpop("https://kpop.xxx/playlists/10346/6-inflw/")

    names = [c["filename"] for c in result["contents"]]
    # the repeat takes its video id as the suffix, the same way vidara's folder
    # entries take their file code
    assert names == ["same name.mp4", "same name 101.mp4"]

def test_a_playlist_with_no_videos_is_an_error(hosts, monkeypatch):
    module = hosts["kpop"]
    _use(module, monkeypatch, {"kpop/playlist": _kpop_playlist(videos=0)})

    with pytest.raises(Exception) as excinfo:
        module.kpop("https://kpop.xxx/playlists/10346/6-inflw/")
    assert "no videos" in str(excinfo.value)

def test_a_kpop_video_is_one_entry_with_its_link(hosts, monkeypatch):
    module = hosts["kpop"]
    _use(
        module,
        monkeypatch,
        {
            "scrape/kpop": {
                "success": True,
                "data": {
                    "video_id": "760625",
                    "title": "a clip",
                    "video_url": "https://cdn.test/a.mp4?verify=abc",
                },
            }
        },
    )

    result = module.kpop("https://kpop.xxx/video/760625/x/")

    assert "lazy" not in result
    assert result["contents"][0]["url"] == "https://cdn.test/a.mp4?verify=abc"
