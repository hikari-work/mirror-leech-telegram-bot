"""Tests for the private seedbox host: what a listing turns into, and what it
deliberately leaves behind.

The host is an nginx autoindex behind HTTP basic auth, so the interesting parts
are the ones a scraper gets wrong quietly: the sort links in the header are
anchors too, the parent entry is a link up rather than a file, names arrive
percent-encoded, and a subdirectory is a row that looks exactly like a file
except for one character. Each of those would otherwise be handed to aria2 as
something to fetch.

The listing below is a trimmed snapshot of what the server actually answers for
``/x-art/`` (September 2026), sortable header and all.

The credentials here are dummies on purpose: this file is committed, and
``config.py`` -- where the real ones live -- is not.
"""

from __future__ import annotations

from base64 import b64encode

import pytest

HOST = "swift-010.seedbox.vip"
USERNAME = "seedbox-user"
PASSWORD = "s3cret"
FOLDER_URL = f"https://{USERNAME}.{HOST}/x-art/"
FILE_URL = f"{FOLDER_URL}carlie_angel_hd.wmv"

LISTING_HTML = "\n".join([
    "<!DOCTYPE html>",
    "<html>",
    "<head><title>Rapidseedbox WebAccess</title></head>",
    "<body>",
    "<h1>",
    "/x-art/</h1>",
    '<table id="list"><thead><tr>'
    '<th style="width:55%"><a href="?C=N&O=A">File Name</a>&nbsp;'
    '<a href="?C=N&O=D">&nbsp;&darr;&nbsp;</a></th>'
    '<th style="width:20%"><a href="?C=S&O=A">File Size</a>&nbsp;'
    '<a href="?C=S&O=D">&nbsp;&darr;&nbsp;</a></th>'
    '<th style="width:25%"><a href="?C=M&O=A">Date</a>&nbsp;'
    '<a href="?C=M&O=D">&nbsp;&darr;&nbsp;</a></th>'
    "</tr></thead>",
    "<tbody>",
    '<tr><td><a href="../">Parent directory/</a></td>'
    "<td>-</td><td>-</td></tr>",
    '<tr><td><a href="Lea%20Guerlin/">Lea Guerlin/</a></td>'
    "<td>-</td><td>September 22, 2026</td></tr>",
    '<tr><td><a href="capri_the_day_we_met_hd.wmv" '
    'title="capri_the_day_we_met_hd.wmv">capri_the_day_we_met_hd.wmv</a></td>'
    "<td>110.3 MiB</td><td>September 22, 2026</td></tr>",
    '<tr><td><a href="carla_girl_in_my_shower_1920x1080.mov" '
    'title="carla_girl_in_my_shower_1920x1080.mov">'
    "carla_girl_in_my_shower_1920x1080.mov</a></td>"
    "<td>501.7 MiB</td><td>September 22, 2026</td></tr>",
    '<tr><td><a href="My%20Video%20Clip.mp4" title="My Video Clip.mp4">'
    "My Video Clip.mp4</a></td>"
    "<td>1.0 GiB</td><td>September 22, 2026</td></tr>",
    "</tbody></table>",
    "</body>",
    "</html>",
])

FILENAMES = [
    "capri_the_day_we_met_hd.wmv",
    "carla_girl_in_my_shower_1920x1080.mov",
    "My Video Clip.mp4",
]

# What a page holding only anchors would leave: the sort links in the header,
# the parent entry, and nothing to download.
EMPTY_LISTING = (
    "<html><body><h1>empty/</h1><table id=\"list\"><thead><tr><th>"
    '<a href="?C=N&O=A">File Name</a></th></tr></thead><tbody>'
    '<tr><td><a href="../">Parent directory/</a></td><td>-</td><td>-</td></tr>'
    "</tbody></table></body></html>"
)


class _Response:
    """One canned answer from the seedbox."""

    def __init__(
        self,
        text: str = "",
        status: int = 200,
        content_type: str = "text/html",
        headers: dict | None = None,
    ):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": content_type}
        self.headers.update(headers or {})


class _Session:
    """Stand-in for ``requests.Session``: one answer, and a record of the ask.

    The request is half of what this module does -- the credentials have to
    reach the server, or every later file download is a 401 -- so the ask is
    kept rather than just answered.
    """

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    @property
    def request_headers(self) -> dict:
        return self.calls[0]["headers"]


def _configure(
    seedbox,
    hosts: str = HOST,
    username: str = USERNAME,
    password: str = PASSWORD,
):
    """Fill in the Config the fixture left empty."""
    seedbox.Config.SEEDBOX_HOSTS = hosts
    seedbox.Config.SEEDBOX_USERNAME = username
    seedbox.Config.SEEDBOX_PASSWORD = password


def _install(monkeypatch, seedbox, response, **config) -> _Session:
    """Answer every request from *response*, and hand back the session."""
    _configure(seedbox, **config)
    session = _Session(response)
    monkeypatch.setattr(seedbox, "Session", lambda: session)
    return session


def _token(username: str = USERNAME, password: str = PASSWORD) -> str:
    return b64encode(f"{username}:{password}".encode()).decode()


# ── the handler ─────────────────────────────────────────────────────


def test_listing_becomes_contents(seedbox, monkeypatch):
    session = _install(monkeypatch, seedbox, _Response(LISTING_HTML))

    details = seedbox.seedbox(FOLDER_URL)

    assert details["title"] == "x-art"
    assert [c["filename"] for c in details["contents"]] == FILENAMES
    # flat: a subdirectory would be a folder the task does not keep
    assert {c["path"] for c in details["contents"]} == {""}
    assert details["contents"][0]["url"] == f"{FOLDER_URL}capri_the_day_we_met_hd.wmv"
    assert all("Lea%20Guerlin" not in c["url"] for c in details["contents"])
    assert session.request_headers["Authorization"] == f"Basic {_token()}"
    assert session.request_headers["User-Agent"]


def test_header_carries_the_credentials_to_aria2(seedbox, monkeypatch):
    """The listing is read with them, and the files are fetched later by aria2
    -- which never sees the Config, only this line."""
    _install(monkeypatch, seedbox, _Response(LISTING_HTML))

    assert seedbox.seedbox(FOLDER_URL)["header"] == (
        f"Authorization: Basic {_token()}"
    )


def test_total_size_is_the_sum_of_the_sizes_shown(seedbox, monkeypatch):
    _install(monkeypatch, seedbox, _Response(LISTING_HTML))

    details = seedbox.seedbox(FOLDER_URL)

    assert details["total_size"] == sum(
        seedbox.seedbox_parse_size(size)
        for size in ("110.3 MiB", "501.7 MiB", "1.0 GiB")
    )


def test_subdirectories_are_counted_and_left_alone(seedbox, monkeypatch):
    """One link should not become a mirror of the host, and the folder it does
    not download is said out loud rather than passed off as the whole of it."""
    _install(monkeypatch, seedbox, _Response(LISTING_HTML))

    seedbox.seedbox(FOLDER_URL)

    assert any("1 subfolder" in m for m in seedbox.LOGGER.messages)


def test_title_falls_back_to_the_heading(seedbox, monkeypatch):
    """A URL with no directory of its own -- a bare host -- still names the
    folder, from the page's own heading."""
    _install(monkeypatch, seedbox, _Response(LISTING_HTML))

    details = seedbox.seedbox(f"https://{USERNAME}.{HOST}/")

    assert details["title"] == "x-art"


def test_a_listing_of_nothing_is_an_error(seedbox, monkeypatch):
    """Sort links and the parent entry are anchors; a page holding only those
    holds no work, which is a different answer from "no credentials"."""
    _install(monkeypatch, seedbox, _Response(EMPTY_LISTING))

    with pytest.raises(seedbox.DirectDownloadLinkException) as excinfo:
        seedbox.seedbox(FOLDER_URL)

    assert str(excinfo.value).startswith("ERROR:")
    assert "No files" in str(excinfo.value)


def test_the_file_cap_truncates_and_says_so(seedbox, monkeypatch):
    monkeypatch.setattr(seedbox, "SEEDBOX_MAX_FILES", 2)
    _install(monkeypatch, seedbox, _Response(LISTING_HTML))

    details = seedbox.seedbox(FOLDER_URL)

    assert [c["filename"] for c in details["contents"]] == FILENAMES[:2]
    assert any("SEEDBOX_MAX_FILES" in m for m in seedbox.LOGGER.messages)


# ── a link that is already a file ───────────────────────────────────


def test_a_file_on_the_host_is_downloaded_as_one(seedbox, monkeypatch):
    """The probe that decides whether to scrape runs unauthenticated, so every
    path on this host looks like a page until it is fetched properly. What
    comes back is the file itself, and all it was missing is the header."""
    _install(
        monkeypatch,
        seedbox,
        _Response(
            "",
            content_type="video/x-ms-wmv",
            headers={"Content-Length": "152242127"},
        ),
    )

    details = seedbox.seedbox(FILE_URL)

    assert details["contents"] == [
        {"path": "", "filename": "carlie_angel_hd.wmv", "url": FILE_URL}
    ]
    assert details["title"] == "carlie_angel_hd"
    assert details["total_size"] == 152242127
    assert details["header"] == f"Authorization: Basic {_token()}"


# ── refusals ────────────────────────────────────────────────────────


def test_missing_credentials_are_reported(seedbox, monkeypatch):
    _install(monkeypatch, seedbox, _Response(LISTING_HTML), password="")

    with pytest.raises(seedbox.DirectDownloadLinkException) as excinfo:
        seedbox.seedbox(FOLDER_URL)

    assert str(excinfo.value).startswith("ERROR:")
    assert "SEEDBOX_PASSWORD" in str(excinfo.value)


def test_rejected_credentials_are_reported(seedbox, monkeypatch):
    _install(monkeypatch, seedbox, _Response(status=401))

    with pytest.raises(seedbox.DirectDownloadLinkException) as excinfo:
        seedbox.seedbox(FOLDER_URL)

    assert str(excinfo.value).startswith("ERROR:")
    assert "credentials" in str(excinfo.value)


def test_a_missing_directory_is_reported(seedbox, monkeypatch):
    _install(monkeypatch, seedbox, _Response(status=404))

    with pytest.raises(seedbox.DirectDownloadLinkException) as excinfo:
        seedbox.seedbox(FOLDER_URL)

    assert str(excinfo.value).startswith("ERROR:")
    assert "404" in str(excinfo.value)


def test_a_dead_connection_is_reported(seedbox, monkeypatch):
    _install(monkeypatch, seedbox, ConnectionError("no route to host"))

    with pytest.raises(seedbox.DirectDownloadLinkException) as excinfo:
        seedbox.seedbox(FOLDER_URL)

    assert str(excinfo.value).startswith("ERROR:")
    assert "no route to host" in str(excinfo.value)


# ── predicate ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (FOLDER_URL, True),
        (f"https://{HOST}/", True),
        (f"https://{HOST}", True),
        # a host that merely ends in the configured one is a different host
        (f"https://{HOST}.evil.com/x-art/", False),
        (f"https://evil.com/{HOST}/x-art/", False),
        # the parent domain is not the configured host either
        ("https://seedbox.vip/x-art/", False),
        ("https://example.com/", False),
        ("not-a-url", False),
    ],
)
def test_host_matching(seedbox, url, expected):
    _configure(seedbox)

    assert seedbox.is_seedbox_link(url) is expected


def test_hosts_may_be_separated_either_way(seedbox):
    _configure(seedbox, hosts="one.example, two.example  three.example")

    assert seedbox.seedbox_hosts() == [
        "one.example",
        "two.example",
        "three.example",
    ]
    assert seedbox.is_seedbox_link("https://deep.two.example/folder/")


def test_nothing_matches_while_the_feature_is_off(seedbox):
    """Empty means the feature is off, and every other handler keeps its links."""
    _configure(seedbox, hosts="")

    assert seedbox.seedbox_hosts() == []
    assert seedbox.is_seedbox_link(FOLDER_URL) is False


def test_a_config_without_the_seedbox_keys_is_not_a_crash(seedbox, monkeypatch):
    """This predicate runs over every link the bot resolves, through a registry
    whose own tests stub a Config that has never heard of these keys."""
    monkeypatch.setattr(seedbox, "Config", type("Config", (), {}))

    assert seedbox.is_seedbox_link(FOLDER_URL) is False
    assert seedbox.seedbox_auth() is None
    assert seedbox.seedbox_auth_header() is None


def test_a_non_string_link_is_not_a_seedbox_link(seedbox):
    _configure(seedbox)

    assert seedbox.is_seedbox_link({"seedbox": True}) is False
    assert seedbox.is_seedbox_link(None) is False


# ── the parts on their own ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 KiB", 2048),
        ("1.0 GiB", 1024**3),
        ("110.3 MiB", int(110.3 * 1024**2)),
        # the decimal spelling of a binary bucket: the difference is 5% on a
        # number that is only ever shown as progress
        ("1 MB", 1024**2),
        # a directory's size, and the shapes that mean nothing
        ("-", 0),
        ("", 0),
        (None, 0),
        ("nonsense", 0),
        ("12", 0),
    ],
)
def test_size_parsing(seedbox, text, expected):
    assert seedbox.seedbox_parse_size(text) == expected


def test_listing_parsing_reports_the_directories_it_skipped(seedbox):
    files, directories = seedbox.seedbox_parse_listing(LISTING_HTML, FOLDER_URL)

    assert [f["filename"] for f in files] == FILENAMES
    assert directories == 1


def test_parsed_names_come_back_decoded(seedbox):
    files, _ = seedbox.seedbox_parse_listing(LISTING_HTML, FOLDER_URL)

    assert files[-1]["filename"] == "My Video Clip.mp4"
    assert files[-1]["url"] == f"{FOLDER_URL}My%20Video%20Clip.mp4"


def test_a_stem_keeps_its_separators_out_of_the_folder_name(seedbox):
    """The title becomes the folder the download lands in, and it comes from a
    URL anyone can type."""
    assert seedbox.seedbox_stem("x-art") == "x-art"

    for value in ("../../etc", "a/b", "..", "", "   "):
        stem = seedbox.seedbox_stem(value)
        assert "/" not in stem and "\\" not in stem
        assert not stem.startswith(".")
