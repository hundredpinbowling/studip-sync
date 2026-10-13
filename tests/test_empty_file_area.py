"""Courses with nothing to sync: an empty file area, or none at all.

Two cases this fork reports instead of hiding. An empty file area makes
`extract_files_flat_last_edit` return 0, which upstream read as "last edit
unknown" and re-downloaded every run; a course with the Dateien tool switched
off answers 403, which upstream swallowed without a word.
"""
import pytest

from studip_sync.session import MissingFeatureError, Session, URL


class FakeResponse:

    url = "https://studip.uni-goettingen.de/dispatch.php/course/files/flat"

    def __init__(self, html, status_code=200):
        self.text = html
        self.status_code = status_code
        self.ok = status_code == 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def files_flat_session():
    """A Session whose files_flat page returns the given response."""

    def build(response):
        session = Session.__new__(Session)
        session.url = URL("https://studip.uni-goettingen.de")
        session.get = lambda *args, **kwargs: response
        return session

    return build


EMPTY_JSON_PAGE = '<form id="files_table_form" data-files="[]"></form>'
EMPTY_TABLE_PAGE = """
<form action="/dispatch.php/course/files/flat">
  <table><tbody><tr><td>Keine Dateien vorhanden.</td></tr></tbody></table>
</form>
"""
FILLED_PAGE = ('<form id="files_table_form" '
               'data-files=\'[{"chdate": 1760000000}]\'></form>')


@pytest.mark.parametrize("html", [EMPTY_JSON_PAGE, EMPTY_TABLE_PAGE])
def test_an_empty_file_area_is_not_resynced(files_flat_session, html, capsys):
    session = files_flat_session(FakeResponse(html))

    assert session.check_course_new_files("course-id", last_sync=0) is False
    assert "No new files for this course" in capsys.readouterr().out


def test_a_file_newer_than_the_last_sync_is_synced(files_flat_session):
    session = files_flat_session(FakeResponse(FILLED_PAGE))

    assert session.check_course_new_files("course-id", last_sync=1759000000) is True


def test_a_file_older_than_the_last_sync_is_skipped(files_flat_session):
    session = files_flat_session(FakeResponse(FILLED_PAGE))

    assert session.check_course_new_files("course-id", last_sync=1760000001) is False


def test_a_disabled_file_area_raises_missing_feature(files_flat_session):
    """403 plus "Documents" is Stud.IP's answer for a deactivated Dateien tool;
    studip_rsync turns this into the "no file area" line."""
    session = files_flat_session(
        FakeResponse("Documents tool is not activated", status_code=403))

    with pytest.raises(MissingFeatureError):
        session.check_course_new_files("course-id", last_sync=0)
