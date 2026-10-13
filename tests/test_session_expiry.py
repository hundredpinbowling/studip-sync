"""A Stud.IP session dropped mid-run, as diagnosed on 2026-10-12.

A `--full` sync ran for 36 minutes, synced 189 files across six courses, and
then — one second after the last successful download — got the login page back
instead of course 7's file index. Stud.IP serves that page with HTTP 200, so
the session layer waved it through, the parser hit `None.attrs`, and the run
died with the whole login page on stdout followed by an `AttributeError`,
leaving every remaining course unsynced.

These tests pin the three parts of the fix: the session layer recognises the
login wall, the parser reports a missing form as a ParserError, and one course
hitting an expired session costs a re-login rather than the run.
"""
import pytest

LOGIN_URL = "https://studip.uni-goettingen.de/dispatch.php/login?cancel_login=1&again=yes"

# Trimmed from a real capture. Note there is no files_table_form, the body is
# German here but renders in English for some accounts, and the only
# language-independent marker of being logged out is USER_ID: "nobody".
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="de"><head><title>Stud.IP - Anmelden</title>
<script>
    var STUDIP = {
        ABSOLUTE_URI_STUDIP: "https://studip.uni-goettingen.de/",
        USER_ID: "nobody",
        server_timestamp: 1791796024
    };
</script></head>
<body><nav><a href="dispatch.php/my_courses">Veranstaltungen</a></nav>
<form action="index.php" method="post">
<input name="loginname"><input type="password" name="password">
<button name="Login" value="Anmelden">Anmelden</button>
</form>
<p>Bitte melden Sie sich an.</p>
</body></html>
"""

COURSE_PAGE = """<!DOCTYPE html>
<html><body><form id="files_table_form" data-files='[]' data-folders='[]'></form>
</body></html>
"""


class FakeResponse:
    """Enough of requests.Response for the paths under test."""

    def __init__(self, url, text, status_code=200):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.ok = status_code < 400
        self.raw = None

    def json(self):
        import json

        return json.loads(self.text)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def html_session():
    """A Session whose every request answers with one canned response."""
    from studip_sync.session import Session, URL

    def build(response):
        class FakeHTTP:
            def __init__(self):
                self.requests = []

            def get(self, url, **kwargs):
                self.requests.append(url)
                return response

            def post(self, url, **kwargs):
                self.requests.append(url)
                return response

        session = Session.__new__(Session)
        session.session = FakeHTTP()
        session.url = URL("https://studip.uni-goettingen.de")
        return session

    return build


@pytest.fixture
def logged_out_session(html_session):
    return html_session(FakeResponse(LOGIN_URL, LOGIN_PAGE))


# Every request the sync makes against a course, by the method that makes it.
# All of them can be the one that arrives a second after the session died.
REQUESTS = {
    "get_files_index": lambda session, tmp: session.get_files_index("course-id"),
    "get_files_index/subfolder":
        lambda session, tmp: session.get_files_index("course-id", "folder-id"),
    "check_course_new_files": lambda session, tmp: session.check_course_new_files("course-id", 0),
    "download": lambda session, tmp: session.download("course-id", str(tmp)),
    "download_file":
        lambda session, tmp: session.download_file("https://studip.uni-goettingen.de/sendfile.php",
                                                   str(tmp / "file")),
    "download_media":
        lambda session, tmp: session.download_media("course-id", str(tmp / "media"), "A course"),
    "get_files_index_from_api": lambda session, tmp: session.get_files_index_from_api("course-id"),
    "download_file_api":
        lambda session, tmp: session.download_file_api("file-id", str(tmp / "file")),
}


@pytest.mark.parametrize("request_name", sorted(REQUESTS))
def test_the_login_wall_is_a_session_expiry(logged_out_session, tmp_path, request_name):
    from studip_sync.session import SessionExpiredError

    with pytest.raises(SessionExpiredError, match="session expired"):
        REQUESTS[request_name](logged_out_session, tmp_path)


@pytest.mark.parametrize("request_name", sorted(REQUESTS))
def test_an_expired_session_does_not_print_the_login_page(logged_out_session, tmp_path, capsys,
                                                          request_name):
    """The original symptom: a screenful of HTML ahead of the traceback."""
    from studip_sync.session import SessionExpiredError

    with pytest.raises(SessionExpiredError):
        REQUESTS[request_name](logged_out_session, tmp_path)

    output = capsys.readouterr().out
    assert "Bitte melden Sie sich an" not in output
    assert "<!DOCTYPE html>" not in output


def test_a_status_200_login_page_is_not_mistaken_for_a_course_page(logged_out_session):
    """Stud.IP serves the login page as HTTP 200, which is why only the 403
    cases were handled before and this one reached the parser."""
    from studip_sync.parsers import ParserError
    from studip_sync.session import SessionExpiredError

    with pytest.raises(SessionExpiredError) as excinfo:
        logged_out_session.get_files_index("course-id")

    assert not isinstance(excinfo.value, (ParserError, AttributeError))
    assert "expired" in str(excinfo.value).lower()


def test_expiry_is_a_download_error():
    """So the --old bulk path, which has no retry, still records a failed
    course and moves on instead of aborting on an unhandled exception."""
    from studip_sync.session import DownloadError, SessionError, SessionExpiredError

    assert issubclass(SessionExpiredError, DownloadError)
    assert issubclass(SessionExpiredError, SessionError)


def test_a_live_session_is_left_alone(html_session):
    """The wall check must not fire on an ordinary course page."""
    session = html_session(FakeResponse(
        "https://studip.uni-goettingen.de/dispatch.php/course/files?cid=course-id", COURSE_PAGE))

    assert session.get_files_index("course-id") == ([], [])


def test_a_real_access_error_is_still_a_missing_feature(html_session):
    """403 handling, which did work, keeps working."""
    from studip_sync.session import MissingFeatureError

    session = html_session(FakeResponse(
        "https://studip.uni-goettingen.de/dispatch.php/course/files?cid=course-id",
        "Documents not activated", status_code=403))

    with pytest.raises(MissingFeatureError):
        session.get_files_index("course-id")


class TestParsers:

    def test_a_missing_files_form_is_a_parser_error(self):
        """It was a bare AttributeError from `None.attrs` — not even reported
        as a parse failure, so nothing upstream could classify it."""
        from studip_sync.parsers import ParserError, extract_files_index_data

        with pytest.raises(ParserError, match="files_table_form"):
            extract_files_index_data("<html><body>no form here</body></html>")

    def test_an_unexpected_page_is_still_dumped(self, capsys):
        """log_html_on_exception earns its keep on a page that needs reading:
        a course page whose markup moved, say."""
        from studip_sync.parsers import ParserError, extract_files_index_data

        html = "<html><body><form id='files_table_form' data-files='[]'></form></body></html>"

        with pytest.raises(ParserError):
            extract_files_index_data(html)

        assert "files_table_form" in capsys.readouterr().out

    def test_the_logged_out_page_is_not_dumped(self, capsys):
        """A backstop under the session-layer check: whatever reaches a parser
        with the login page, the page itself explains nothing."""
        from studip_sync.parsers import ParserError, extract_files_index_data

        with pytest.raises(ParserError):
            extract_files_index_data(LOGIN_PAGE)

        output = capsys.readouterr().out
        assert "Bitte melden Sie sich an" not in output
        assert "logged out" in output.lower()

    def test_the_logged_out_page_is_recognised_by_the_user_id(self):
        """Not by body text: the page renders in German or English depending
        on account state, so a marker that survives both is required."""
        from studip_sync.parsers import is_logged_out_page

        assert is_logged_out_page(LOGIN_PAGE)
        assert is_logged_out_page('var STUDIP = { USER_ID: "nobody" };')
        assert not is_logged_out_page(COURSE_PAGE)
        assert not is_logged_out_page('var STUDIP = { USER_ID: "abc123" };')


def test_an_expiry_deep_in_a_course_reaches_the_caller(html_session, tmp_path):
    """The real failure was in a sub-folder, frames down the recursion of
    CourseRSync.download_recursive — which swallows an exception class of its
    own, so the expiry has to pass it untouched to be retryable at all."""
    from studip_sync.session import SessionExpiredError
    from studip_sync.studip_rsync import CourseRSync

    pages = [
        FakeResponse("https://studip.uni-goettingen.de/dispatch.php/course/files?cid=course-id",
                     """<html><body><form id="files_table_form" data-files='[]'
                        data-folders='[{"id": "deadbeef", "name": "Slides"}]'></form>
                        </body></html>"""),
        FakeResponse(LOGIN_URL, LOGIN_PAGE),
    ]
    session = html_session(None)
    session.session.get = lambda url, **kwargs: pages.pop(0)

    course_rsync = CourseRSync(session, str(tmp_path), str(tmp_path / "A course"),
                               {"course_id": "course-id", "save_as": "A course"},
                               sync_fully=True, use_api=False)

    with pytest.raises(SessionExpiredError):
        course_rsync.download()


class FakeSession:
    """Records logins, and can refuse them."""

    def __init__(self, login_fails=False, courses=()):
        self.logins = 0
        self.login_fails = login_fails
        self.courses = list(courses)

    def login(self, auth_type, auth_type_data, username, password):
        from studip_sync.logins import LoginError

        self.logins += 1
        if self.login_fails:
            raise LoginError("Wrong credentials, cannot login")

    def get_courses(self, only_recent_semester=False):
        return self.courses

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def expire_once():
    """An action that raises SessionExpiredError on its first call only."""
    from studip_sync.session import SessionExpiredError

    calls = []

    def action():
        calls.append(1)
        if len(calls) == 1:
            raise SessionExpiredError("Stud.IP session expired: the request landed on "
                                      + LOGIN_URL)
        return "done"

    action.calls = calls
    return action


class TestRetry:
    """retry_once_after_relogin, which the file and media paths both use."""

    def test_a_healthy_request_never_logs_in_again(self):
        from studip_sync.studip_rsync import retry_once_after_relogin

        session = FakeSession()

        assert retry_once_after_relogin(session, lambda: "done") == "done"
        assert session.logins == 0

    def test_an_expired_session_is_renewed_and_the_action_retried(self, capsys):
        from studip_sync.studip_rsync import retry_once_after_relogin

        session = FakeSession()
        action = expire_once()

        assert retry_once_after_relogin(session, action) == "done"
        assert session.logins == 1
        assert len(action.calls) == 2
        assert "session expired" in capsys.readouterr().out.lower()

    def test_a_failed_re_login_says_so(self):
        """Nothing is recoverable past this point, and the message has to name
        the reason rather than read like an ordinary download failure."""
        from studip_sync.session import SessionExpiredError
        from studip_sync.studip_rsync import retry_once_after_relogin

        session = FakeSession(login_fails=True)

        with pytest.raises(SessionExpiredError, match="re-login failed"):
            retry_once_after_relogin(session, expire_once())

        assert session.logins == 1

    def test_expiring_again_after_a_fresh_login_is_not_retried_forever(self):
        """One retry only: a session that dies again immediately is not the
        transient drop this handles."""
        from studip_sync.session import SessionExpiredError
        from studip_sync.studip_rsync import retry_once_after_relogin

        session = FakeSession()

        def always_expired():
            raise SessionExpiredError("Stud.IP session expired")

        with pytest.raises(SessionExpiredError):
            retry_once_after_relogin(session, always_expired)

        assert session.logins == 1


COURSES = [
    {"course_id": "c01", "save_as": "Generalized Regression", "semester": "WiSe 2026/27",
     "semester_id": 5},
    {"course_id": "c02", "save_as": "Multivariate Statistics", "semester": "WiSe 2026/27",
     "semester_id": 5},
    {"course_id": "c03", "save_as": "Datenbanken", "semester": "WiSe 2026/27", "semester_id": 5},
]


class TestWholeRun:
    """What the 2026-10-12 run really cost: every course after the expiry."""

    @pytest.fixture
    def run_sync(self, monkeypatch):
        """Run StudIPRSync.sync over three courses, CourseRSync faked out.

        `run(expiries={"Multivariate Statistics": 1})` makes that course's
        download() raise SessionExpiredError once before succeeding, the way
        course 7 of the real run did. Returns (status_code, attempts, session,
        last_sync), where `attempts` holds one (title, destination directory)
        entry per download() call.
        """
        from studip_sync import studip_rsync
        from studip_sync.config import get_config
        from studip_sync.session import SessionExpiredError

        attempts = []
        last_sync = []

        def build(expiries=None):
            expiries = dict(expiries or {})

            def fake_course_rsync(session, workdir, root_folder, course, sync_fully, use_api):
                class FakeCourseRSync:
                    def download(self):
                        attempts.append((course["save_as"], root_folder))
                        if expiries.get(course["save_as"]):
                            expiries[course["save_as"]] -= 1
                            raise SessionExpiredError("Stud.IP session expired: the request "
                                                      "landed on " + LOGIN_URL)

                return FakeCourseRSync()

            session = FakeSession(courses=COURSES)

            monkeypatch.setattr(studip_rsync, "CourseRSync", fake_course_rsync)
            monkeypatch.setattr(studip_rsync, "Session", lambda **kwargs: session)
            monkeypatch.setattr(get_config(), "update_last_sync", last_sync.append)

            with studip_rsync.StudIPRSync() as rsync:
                status_code = rsync.sync(sync_fully=True, use_api=False)

            return status_code, attempts, session, last_sync

        return build

    def test_an_undisturbed_run_syncs_every_course_once(self, run_sync):
        status_code, attempts, session, last_sync = run_sync()

        assert status_code == 0
        assert [title for title, _ in attempts] == [course["save_as"] for course in COURSES]
        assert session.logins == 1
        assert len(last_sync) == 1

    def test_one_expiry_costs_a_re_login_not_the_rest_of_the_run(self, run_sync):
        status_code, attempts, session, last_sync = run_sync(
            expiries={"Multivariate Statistics": 1})

        assert status_code == 0
        assert [title for title, _ in attempts] == [
            "Generalized Regression",
            "Multivariate Statistics",   # died here, mid-list, as on 2026-10-12
            "Multivariate Statistics",   # retried, rather than ending the run
            "Datenbanken",
        ]
        assert session.logins == 2
        assert len(last_sync) == 1

    def test_the_retry_resyncs_the_same_course_into_the_same_directory(self, run_sync):
        _, attempts, _, _ = run_sync(expiries={"Multivariate Statistics": 1})

        retried = [entry for entry in attempts if entry[0] == "Multivariate Statistics"]

        assert len(retried) == 2
        assert retried[0] == retried[1]

    def test_an_unrenewable_session_stops_on_a_message_not_a_traceback(self, run_sync, capsys):
        """A second expiry straight after a fresh login is unrecoverable, but
        raising through main() is the one thing this must not do."""
        status_code, attempts, session, last_sync = run_sync(
            expiries={"Multivariate Statistics": 2})

        output = capsys.readouterr().out

        assert status_code == 2
        assert "Stopping" in output
        assert "<!DOCTYPE html>" not in output
        assert [title for title, _ in attempts] == ["Generalized Regression",
                                                    "Multivariate Statistics",
                                                    "Multivariate Statistics"]
        assert last_sync == [], "a failed run must not advance last_sync"

    def test_a_refused_re_login_stops_the_run_too(self, run_sync, monkeypatch, capsys):
        from studip_sync.logins import LoginError

        def fail_every_login_after_the_first(self, *args):
            self.logins += 1
            if self.logins > 1:
                raise LoginError("Wrong credentials, cannot login")

        monkeypatch.setattr(FakeSession, "login", fail_every_login_after_the_first)

        status_code, _, _, last_sync = run_sync(expiries={"Multivariate Statistics": 1})

        assert status_code == 2
        assert "re-login failed" in capsys.readouterr().out
        assert last_sync == []
