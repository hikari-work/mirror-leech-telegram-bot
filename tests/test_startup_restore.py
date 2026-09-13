"""What ``restore_users`` rebuilds, and what it does not have to ask for.

``restore_users`` merges two sources: the scalar rows in ``users``, and the
names of the blobs under ``users/<uid>/<KEY>``. The second source matters
because a user can own a file without owning a row -- uploading a thumbnail
calls ``update_user_doc``, never ``update_user_data`` -- so those ids exist
only as blob names. ``load_settings`` wipes ``thumbnails/`` on every boot and
rebuilds it from here, so a user this function misses loses their thumbnail
until they upload it again.

Two properties are pinned, and the fake store is what makes either of them
mean anything:

* the merge finds those ids at all, which is about the *shape* of the names
  ``list_blobs`` answers with -- so the fake strips a prefix the way the real
  store does (``list_blobs`` returns names with the root it was given removed,
  which was verified against PostgreSQL, not assumed);
* a user with no stored file is not asked for. ``get_blob`` is one round trip
  each, and this runs before the bot answers anything, so it used to cost one
  per user whether or not the user had a file to fetch.

``bot.core.startup`` is imported for real rather than stubbed: it pulls the bot
package and about a second and a half of imports, which this suite pays
elsewhere too, and the alternative -- slicing the function out of its source --
would test a copy of it. What is replaced is the module's ``database``, its
``user_data``, and the working directory, since ``thumbnails/`` paths are
relative and must not land in the repo.
"""

from __future__ import annotations

import pytest

from bot.core import startup


class _FakeStore:
    """The slice of ``DbManager`` this function reads, with its name handling.

    ``names`` is held the way the database holds them -- ``users/<uid>/<KEY>``
    for a user file, a flat path for anything else -- and ``list_blobs``
    removes whatever root it was asked for, as the real one does. Without that
    the merge could not fail, and a test of it would prove nothing.
    """

    def __init__(self, rows=(), names=(), presets=None, payloads=None):
        self._rows = list(rows)
        self._names = list(names)
        self._presets = dict(presets or {})
        self._payloads = dict(payloads or {})
        self.prefixes: list[str] = []
        self.blob_reads: list[str] = []
        self.preset_reads = 0

    async def read_user_rows(self):
        return list(self._rows)

    async def list_blobs(self, prefix="", bot_id=None):
        self.prefixes.append(prefix)
        return [n[len(prefix) :] for n in self._names if n.startswith(prefix)]

    async def read_copy_presets_all(self):
        self.preset_reads += 1
        return dict(self._presets)

    async def get_blob(self, name, bot_id=None):
        self.blob_reads.append(name)
        return self._payloads.get(name)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Wire the restores to a fake store, a fresh ``user_data`` and a tmp cwd.

    A factory rather than a ready-made object, because what the store holds is
    the whole of each test's setup. ``thumbnails/`` paths are relative, so the
    working directory moves too -- otherwise these tests write into the repo.
    """

    def wire(**kwargs):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(startup, "user_data", {})
        wired = _FakeStore(**kwargs)
        monkeypatch.setattr(startup, "database", wired)
        return wired

    return wire


async def test_a_user_whose_only_record_is_a_stored_file_comes_back(
    store, tmp_path
) -> None:
    """The bug this pins: a thumbnail-only user used to be dropped entirely.

    Nothing in ``users`` mentions them, so the blob name is the only evidence
    they exist -- and the merge that reads it was matching on a name shape the
    listing never produced.
    """
    wired = store(
        names=["users/777/THUMBNAIL"],
        payloads={"users/777/THUMBNAIL": b"pixels"},
    )

    await startup.restore_users("bot")

    assert startup.user_data[777]["THUMBNAIL"] == "thumbnails/777.jpg"
    # not just remembered: the file is on disk, which is what the wipe took
    assert (tmp_path / "thumbnails" / "777.jpg").read_bytes() == b"pixels"
    # asked for with no prefix, which is what keeps the name three parts long
    assert wired.prefixes == [""]


async def test_a_stored_file_is_written_back_for_a_user_that_has_a_row(
    store, tmp_path
) -> None:
    """The merge is an addition to the rows, not a replacement for them."""
    wired = store(
        rows=[(5, {"AS_DOCUMENT": True})],
        names=["users/5/THUMBNAIL", "users/7/THUMBNAIL"],
        payloads={"users/5/THUMBNAIL": b"five", "users/7/THUMBNAIL": b"seven"},
    )

    await startup.restore_users("bot")

    assert startup.user_data[5] == {
        "AS_DOCUMENT": True,
        "THUMBNAIL": "thumbnails/5.jpg",
    }
    assert (tmp_path / "thumbnails" / "5.jpg").read_bytes() == b"five"
    # and the row-less user beside them still lands
    assert startup.user_data[7] == {"THUMBNAIL": "thumbnails/7.jpg"}
    assert wired.blob_reads == ["users/5/THUMBNAIL", "users/7/THUMBNAIL"]


async def test_a_user_with_no_stored_file_costs_no_fetch(store) -> None:
    """Two rows and no user blobs: not one ``get_blob``, which used to be two."""
    wired = store(rows=[(1, {}), (2, {"AS_DOCUMENT": True})])

    await startup.restore_users("bot")

    assert wired.blob_reads == []
    assert startup.user_data == {1: {}, 2: {"AS_DOCUMENT": True}}


async def test_only_the_users_holding_a_thumbnail_are_asked_for(store) -> None:
    """The count that scales, cut to the users who have something to fetch."""
    wired = store(
        rows=[(1, {}), (2, {}), (3, {})],
        names=["users/2/THUMBNAIL", "config.py"],
        payloads={"users/2/THUMBNAIL": b"two"},
    )

    await startup.restore_users("bot")

    assert wired.blob_reads == ["users/2/THUMBNAIL"]
    assert startup.user_data[2] == {"THUMBNAIL": "thumbnails/2.jpg"}
    assert startup.user_data[1] == {}
    assert startup.user_data[3] == {}


async def test_a_blob_name_that_is_not_a_user_file_is_not_a_user(store) -> None:
    """Flat names are the rest of the store's payload, not user ids."""
    store(names=["config.py", "rclone.conf", "nested/x"])

    await startup.restore_users("bot")

    assert startup.user_data == {}


async def test_an_empty_store_reads_no_presets(store) -> None:
    """Nothing to restore means nothing to overlay, presets included."""
    wired = store(names=["config.py"])

    await startup.restore_users("bot")

    assert startup.user_data == {}
    assert wired.preset_reads == 0


async def test_presets_are_overlaid_on_the_row_they_belong_to(store) -> None:
    """The other half of what this rebuilds, pinned so the merge change is safe."""
    store(rows=[(5, {"AS_DOCUMENT": True})], presets={5: {"anime": ["pm"]}})

    await startup.restore_users("bot")

    assert startup.user_data[5] == {
        "AS_DOCUMENT": True,
        "COPY_PRESETS": {"anime": ["pm"]},
    }


async def test_a_user_holding_only_a_file_gets_presets_too(store) -> None:
    """The two overlays meet on the row the merge just created."""
    store(
        names=["users/7/THUMBNAIL"],
        payloads={"users/7/THUMBNAIL": b"seven"},
        presets={7: {"anime": ["pm"]}},
    )

    await startup.restore_users("bot")

    assert startup.user_data[7] == {
        "THUMBNAIL": "thumbnails/7.jpg",
        "COPY_PRESETS": {"anime": ["pm"]},
    }
