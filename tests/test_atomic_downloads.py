"""A download is either absent or complete, never truncated.

studip-sync streamed every download straight onto its final path, so anything
that cut a transfer short — a reset connection, a full disk, Ctrl-C during the
40-minute sync that prompted tests/test_session_expiry.py — left a partial
file under the name the complete one would have.

studip-sync/studip-sync#36 is where the fix comes from: it wrapped the
streaming downloads in session.py in a temp file plus os.replace. These tests
pin that behaviour, two bugs in that PR's version of it (a partial file leaked
when the copy itself fails, and no cleanup on KeyboardInterrupt), and the
write the PR does not touch — the copy into the synced library, which is the
one the user keeps.
"""
import io
import os

import pytest


class Interrupted(Exception):
    """A transfer that dies partway through, the way a reset connection does."""


class HalfBody:
    """A response body that yields `prefix` and then raises.

    shutil.copyfileobj reads in chunks, so a body has to be able to hand over
    real bytes before failing — a stream that fails on the first read would
    not distinguish a partial write from no write at all.
    """

    def __init__(self, prefix=b"PK\x03\x04 the first half", error=Interrupted("connection reset")):
        self.prefix = prefix
        self.error = error
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        if self.reads == 1:
            return self.prefix
        raise self.error


class FakeResponse:
    """Enough of a streamed requests.Response for the download paths."""

    ok = True
    status_code = 200

    def __init__(self, body, url="https://studip.uni-goettingen.de/sendfile.php"):
        self.url = url
        self.raw = io.BytesIO(body) if isinstance(body, bytes) else body
        self.text = ""
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def leftovers(directory):
    """Half-written temp files still lying about in `directory`."""
    return [name for name in os.listdir(directory) if name.startswith(".part-")]


class TestStreamToFile:

    def test_a_complete_stream_lands_at_the_path(self, tmp_path):
        from studip_sync.atomic import stream_to_file

        target = tmp_path / "Vorlesung 1.pdf"
        stream_to_file(FakeResponse(b"%PDF-1.7 slides"), str(target))

        assert target.read_bytes() == b"%PDF-1.7 slides"
        assert leftovers(tmp_path) == []

    def test_an_interrupted_stream_leaves_no_file_at_the_path(self, tmp_path):
        """The whole point: a partial transfer must not be mistakable for a
        downloaded file."""
        from studip_sync.atomic import stream_to_file

        target = tmp_path / "Vorlesung 1.pdf"

        with pytest.raises(Interrupted):
            stream_to_file(FakeResponse(HalfBody()), str(target))

        assert not target.exists()

    def test_an_interrupted_stream_leaves_no_partial_file_behind(self, tmp_path):
        """Not just at the target name — anywhere. studip-sync/studip-sync#36
        records its temp path only after the copy returns, so when the copy is
        what fails, its cleanup has nothing to clean and the .part- file is
        leaked into the user's course directory on every failure."""
        from studip_sync.atomic import stream_to_file

        with pytest.raises(Interrupted):
            stream_to_file(FakeResponse(HalfBody()), str(tmp_path / "Vorlesung 1.pdf"))

        assert os.listdir(tmp_path) == []

    def test_ctrl_c_mid_download_cleans_up_too(self, tmp_path):
        """A sync long enough to be interrupted by hand is the likeliest way
        to get here at all, and KeyboardInterrupt is not an Exception — so an
        `except Exception` cleanup, as in #36, misses exactly that case."""
        from studip_sync.atomic import stream_to_file

        body = HalfBody(error=KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            stream_to_file(FakeResponse(body), str(tmp_path / "Vorlesung 1.pdf"))

        assert os.listdir(tmp_path) == []

    def test_an_interrupted_stream_does_not_touch_the_previous_version(self, tmp_path):
        """A failed re-download must cost nothing. Writing in place truncated
        the copy that was already there and known good."""
        from studip_sync.atomic import stream_to_file

        target = tmp_path / "Vorlesung 1.pdf"
        target.write_bytes(b"the version from last week")

        with pytest.raises(Interrupted):
            stream_to_file(FakeResponse(HalfBody()), str(target))

        assert target.read_bytes() == b"the version from last week"
        assert leftovers(tmp_path) == []

    def test_the_path_never_holds_half_a_file(self, tmp_path):
        """Readers of the course directory — a backup, an indexer, the next
        run — see the old bytes or the new bytes, never a mixture."""
        from studip_sync.atomic import stream_to_file

        target = tmp_path / "Vorlesung 1.pdf"
        target.write_bytes(b"old")

        seen = []

        class WatchingBody:
            def __init__(self):
                self.chunks = [b"new bytes, part one", b"new bytes, part two", b""]

            def read(self, size=-1):
                seen.append(target.read_bytes())
                return self.chunks.pop(0)

        stream_to_file(FakeResponse(WatchingBody()), str(target))

        assert seen == [b"old", b"old", b"old"], "the target was written through mid-stream"
        assert target.read_bytes() == b"new bytes, part onenew bytes, part two"

    def test_the_temp_file_shares_the_destination_filesystem(self, tmp_path):
        """os.replace is only atomic within one filesystem, and a course
        directory on an external disk or a network mount is ordinary for this
        program — so the temp file cannot live in /tmp."""
        from studip_sync.atomic import stream_to_file

        target = tmp_path / "course" / "Vorlesung 1.pdf"
        during = []

        class WatchingBody:
            def __init__(self):
                self.chunks = [b"slides", b""]

            def read(self, size=-1):
                during.append(leftovers(target.parent))
                return self.chunks.pop(0)

        stream_to_file(FakeResponse(WatchingBody()), str(target))

        assert during[0], "no temp file in the destination directory while writing"

    def test_missing_directories_are_created(self, tmp_path):
        from studip_sync.atomic import stream_to_file

        target = tmp_path / "WiSe 2026-27" / "Datenbanken" / "Folien" / "01.pdf"
        stream_to_file(FakeResponse(b"slides"), str(target))

        assert target.read_bytes() == b"slides"


class TestReplaceFile:
    """The copy out of the work directory into the synced library."""

    def test_a_complete_copy_lands_at_the_destination(self, tmp_path):
        from studip_sync.atomic import replace_file

        source = tmp_path / "staged"
        source.write_bytes(b"%PDF-1.7 slides")
        destination = tmp_path / "library" / "Vorlesung 1.pdf"

        replace_file(str(source), str(destination))

        assert destination.read_bytes() == b"%PDF-1.7 slides"
        assert source.exists(), "the staged file is copied, not moved"
        assert leftovers(destination.parent) == []

    def test_a_failed_copy_leaves_the_destination_alone(self, tmp_path, monkeypatch):
        from studip_sync import atomic

        source = tmp_path / "staged"
        source.write_bytes(b"the new version")
        destination = tmp_path / "Vorlesung 1.pdf"
        destination.write_bytes(b"the version from last week")

        def fail_halfway(fsrc, fdst, length=0):
            fdst.write(b"the new ver")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic.shutil, "copyfileobj", fail_halfway)

        with pytest.raises(OSError):
            atomic.replace_file(str(source), str(destination))

        assert destination.read_bytes() == b"the version from last week"
        assert leftovers(tmp_path) == []


class FakeHTTP:
    """A requests.Session stand-in answering every call with one response."""

    def __init__(self, response):
        self.response = response
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


@pytest.fixture
def session():
    from studip_sync.session import Session, URL

    def build(response):
        session = Session.__new__(Session)
        session.session = FakeHTTP(response)
        session.url = URL("https://studip.uni-goettingen.de")
        session.plugins = type("Plugins", (), {"hook": staticmethod(lambda *a, **kw: None)})()
        return session

    return build


class TestSessionDownloads:
    """The session methods, which is where #36 applied the fix."""

    def test_a_downloaded_file_lands_complete(self, session, tmp_path):
        target = tmp_path / "file"
        session(FakeResponse(b"%PDF-1.7 slides")).download_file(
            "https://studip.uni-goettingen.de/sendfile.php", str(target))

        assert target.read_bytes() == b"%PDF-1.7 slides"

    def test_an_interrupted_file_download_leaves_nothing(self, session, tmp_path):
        with pytest.raises(Interrupted):
            session(FakeResponse(HalfBody())).download_file(
                "https://studip.uni-goettingen.de/sendfile.php", str(tmp_path / "file"))

        assert os.listdir(tmp_path) == []

    def test_an_interrupted_api_download_leaves_nothing(self, session, tmp_path):
        with pytest.raises(Interrupted):
            session(FakeResponse(HalfBody())).download_file_api("file-id", str(tmp_path / "file"))

        assert os.listdir(tmp_path) == []

    def test_an_interrupted_bulk_download_leaves_no_half_a_zip(self, session, tmp_path):
        """The --old path unzips what this returns, so a truncated .zip costs
        the run even though it does fail loudly."""
        files_page = FakeResponse(b"")
        files_page.text = """<html><body><form>
            <input name="parent_folder_id" value="deadbeef">
            <input name="security_token" value="csrf">
            </form></body></html>"""
        responses = [files_page, FakeResponse(HalfBody())]

        studip = session(None)
        studip.session.get = lambda url, **kwargs: responses.pop(0)
        studip.session.post = lambda url, **kwargs: responses.pop(0)

        with pytest.raises(Interrupted):
            studip.download("course-id", str(tmp_path))

        assert os.listdir(tmp_path) == []


MEDIACAST_LIST = """<!DOCTYPE html>
<html><body>
<table class="media-table" id="abc123hash">
  <div class="media-table-infos"><div><a href="media/show/abc123hash">Vorlesung 1</a></div></div>
</table>
</body></html>
"""


class TestMediaDownloads:
    """The one path where a truncated file is never noticed again.

    download_media decides what to skip by matching the hash in the filename,
    with no size or timestamp to compare against — so a partial media file
    under its final name is skipped by every later run. Of everything this
    change covers, this is the case that actually loses data.
    """

    @pytest.fixture
    def media_session(self, session):
        def build(body):
            list_page = FakeResponse(b"")
            list_page.text = MEDIACAST_LIST
            download = FakeResponse(body)
            download.headers = {"Content-Disposition": 'attachment; filename="Vorlesung 1.mp4"'}
            responses = [list_page, download]

            studip = session(None)
            studip.session.get = lambda url, **kwargs: responses.pop(0)
            return studip

        return build

    def test_a_complete_media_file_is_saved_under_its_hash(self, media_session, tmp_path):
        media = tmp_path / "media"
        media_session(b"\x00\x00\x00 moov").download_media("course-id", str(media), "Datenbanken")

        assert (media / "Vorlesung 1-abc123hash.mp4").read_bytes() == b"\x00\x00\x00 moov"

    def test_an_interrupted_media_file_is_not_left_for_the_next_run_to_skip(self, media_session,
                                                                           tmp_path):
        media = tmp_path / "media"

        with pytest.raises(Interrupted):
            media_session(HalfBody()).download_media("course-id", str(media), "Datenbanken")

        assert os.listdir(media) == [], "a partial media file would be skipped forever"


class TestTheLibraryCopy:
    """download_recursive's write into the user's course directory.

    Not covered by studip-sync/studip-sync#36, and the one write whose result
    the user actually keeps.
    """

    @pytest.fixture
    def course_rsync(self, tmp_path):
        from studip_sync import studip_rsync

        def build():
            class FakeSession:
                plugins = type("Plugins", (), {"hook": staticmethod(lambda *a, **kw: None)})()

                def get_files_index(self, course_id, folder_id=None):
                    return ([{"id": "abc123", "name": "Vorlesung 1.pdf", "size": 15,
                              "chdate": 2000000000,
                              "download_url": "https://studip.uni-goettingen.de/sendfile.php"}],
                            [])

                def download_file(self, download_url, target_file):
                    with open(target_file, "wb") as file:
                        file.write(b"%PDF-1.7 slides")

            workdir = tmp_path / "workdir"
            workdir.mkdir()
            library = tmp_path / "library"

            return studip_rsync.CourseRSync(
                FakeSession(), str(workdir), str(library),
                {"course_id": "course-id", "save_as": "Datenbanken"},
                sync_fully=True, use_api=False), library

        return build

    def test_a_synced_file_lands_in_the_library(self, course_rsync):
        rsync, library = course_rsync()
        rsync.download_recursive()

        assert (library / "Vorlesung 1.pdf").read_bytes() == b"%PDF-1.7 slides"
        assert leftovers(library) == []

    def test_a_failed_library_copy_keeps_the_previous_version(self, course_rsync, monkeypatch):
        """The .old rename has already happened by this point, so an in-place
        copy that fails here leaves the library with neither version."""
        from studip_sync import atomic

        rsync, library = course_rsync()
        library.mkdir()
        target = library / "Vorlesung 1.pdf"
        target.write_bytes(b"last week's slides")

        def fail_halfway(fsrc, fdst, length=0):
            fdst.write(b"%PDF-1.7 sli")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic.shutil, "copyfileobj", fail_halfway)

        with pytest.raises(OSError):
            rsync.download_recursive()

        backups = sorted(path.name for path in library.iterdir() if path.name.endswith(".old"))

        assert len(backups) == 1, "the previous version was not kept anywhere"
        assert (library / backups[0]).read_bytes() == b"last week's slides"
        assert not target.exists(), "a truncated file is left under the real name"
        assert leftovers(library) == []
