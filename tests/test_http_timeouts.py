"""Every request carries a timeout.

requests applies no default timeout at all, so a server that accepts a
connection and then stops answering holds the sync open for as long as the
TCP stack allows. For a cron'd sync that means an overnight hang with the next
run queued behind it — the same class of problem as the dropped session in
tests/test_session_expiry.py, except nothing is raised and nothing is logged.

Taken from studip-sync/studip-sync#36, which added a flat 30-second timeout to
every request. Split in two here: the read timeout bounds the wait for the
first byte as well as the gaps between bytes, and Stud.IP zips a whole course
server-side before answering a bulk download, so 30s would abort exactly the
downloads that matter most. These tests pin that split, and that no request
path — including login, which #36 also routes through the wrapper — is left
without a timeout.
"""
import pytest


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, url="https://studip.uni-goettingen.de/dispatch.php/start", text=""):
        self.url = url
        self.text = text
        self.raw = None
        self.headers = {}

    def json(self):
        return {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingHTTP:
    """A requests.Session stand-in that records the kwargs of every call."""

    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response

    @property
    def timeouts(self):
        return [kwargs.get("timeout", "MISSING") for _, _, kwargs in self.calls]


@pytest.fixture
def session():
    """A real Session — constructor and all — over a recording transport."""
    from studip_sync.session import Session

    def build(response=None, **kwargs):
        session = Session(**kwargs)
        session.session = RecordingHTTP(response)
        return session

    return build


class TestTheDefaults:

    def test_the_constants_are_ordered_the_way_the_split_requires(self):
        from studip_sync.constants import HTTP_CONNECT_TIMEOUT, HTTP_DOWNLOAD_READ_TIMEOUT, \
            HTTP_READ_TIMEOUT

        assert HTTP_CONNECT_TIMEOUT < HTTP_READ_TIMEOUT < HTTP_DOWNLOAD_READ_TIMEOUT

    def test_a_download_is_given_minutes_not_seconds(self):
        """#36's flat 30s is the value this diverges from: Stud.IP builds the
        .zip before it sends a byte, which takes minutes for a large course."""
        from studip_sync.constants import HTTP_DOWNLOAD_READ_TIMEOUT

        assert HTTP_DOWNLOAD_READ_TIMEOUT >= 300

    def test_a_session_defaults_to_a_connect_and_read_pair(self, session):
        from studip_sync.constants import HTTP_CONNECT_TIMEOUT, HTTP_DOWNLOAD_READ_TIMEOUT, \
            HTTP_READ_TIMEOUT

        studip = session()

        assert studip.timeout == (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
        assert studip.download_timeout == (HTTP_CONNECT_TIMEOUT, HTTP_DOWNLOAD_READ_TIMEOUT)

    def test_the_timeouts_can_be_overridden_per_session(self, session):
        studip = session(connect_timeout=1, read_timeout=2, download_read_timeout=3)

        assert studip.timeout == (1, 2)
        assert studip.download_timeout == (1, 3)


class TestTheWrappers:

    def test_an_ordinary_get_carries_the_short_read_timeout(self, session):
        studip = session()
        studip.get("https://studip.uni-goettingen.de/dispatch.php/start")

        assert studip.session.timeouts == [studip.timeout]

    def test_an_ordinary_post_carries_the_short_read_timeout(self, session):
        studip = session()
        studip.post("https://studip.uni-goettingen.de/index.php", data={})

        assert studip.session.timeouts == [studip.timeout]

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_a_streamed_request_carries_the_long_one(self, session, method):
        """stream=True marks exactly the requests whose first byte is slow."""
        studip = session()
        getattr(studip, method)("https://studip.uni-goettingen.de/sendfile.php", stream=True)

        assert studip.session.timeouts == [studip.download_timeout]

    def test_stream_is_still_passed_through(self, session):
        """Keying the timeout off `stream` must not consume it: without it
        requests would buffer whole files into memory."""
        studip = session()
        studip.get("https://studip.uni-goettingen.de/sendfile.php", stream=True)

        assert studip.session.calls[0][2]["stream"] is True

    def test_an_explicit_timeout_wins(self, session):
        studip = session()
        studip.get("https://studip.uni-goettingen.de/dispatch.php/start", timeout=5)

        assert studip.session.timeouts == [5]

    def test_an_explicit_timeout_is_not_passed_twice(self, session):
        """It is popped out of kwargs rather than left there — otherwise
        requests gets `timeout` twice and raises TypeError."""
        studip = session()
        studip.get("https://studip.uni-goettingen.de/sendfile.php", stream=True, timeout=5)

        assert studip.session.calls[0][2]["timeout"] == 5

    def test_other_kwargs_are_untouched(self, session):
        studip = session()
        studip.get("https://studip.uni-goettingen.de/dispatch.php/course/files",
                   params={"cid": "course-id"})

        assert studip.session.calls[0][2]["params"] == {"cid": "course-id"}


# Enough of Göttingen's login page for GeneralLogin.extract_login_data to find
# a form, so that the login path gets as far as its second and third requests.
LOGIN_PAGE = """<!DOCTYPE html>
<html><body>
<form action="https://studip.uni-goettingen.de/index.php" method="post">
<input name="loginname"><input type="password" name="password">
<input type="hidden" name="login_ticket" value="ticket">
<button name="Login" value="Anmelden">Anmelden</button>
</form>
</body></html>
"""


# Every request path in the program, by the call that makes it. The timeout
# belongs in the wrapper precisely so that this list cannot drift out of date:
# a new request has to opt out to go without one.
REQUEST_PATHS = {
    "check_course_new_files": lambda studip: studip.check_course_new_files("course-id", 0),
    "get_files_index": lambda studip: studip.get_files_index("course-id"),
    "get_files_index/subfolder": lambda studip: studip.get_files_index("course-id", "folder-id"),
    "get_files_index_from_api": lambda studip: studip.get_files_index_from_api("course-id"),
    "jsonapi_get": lambda studip: studip.jsonapi_get("users/me"),
    "download_file": lambda studip: studip.download_file(
        "https://studip.uni-goettingen.de/sendfile.php", studip.target),
    "download_file_api": lambda studip: studip.download_file_api("file-id", studip.target),
    "login": lambda studip: studip.login("general", {}, "user", "password"),
}


@pytest.mark.parametrize("path", sorted(REQUEST_PATHS))
def test_no_request_path_goes_without_a_timeout(session, tmp_path, path):
    """Including login, which reached requests directly before this change —
    a hang there is the worst case of all, since it predates every course."""
    studip = session(FakeResponse(text=LOGIN_PAGE))
    studip.target = str(tmp_path / "file")

    try:
        REQUEST_PATHS[path](studip)
    except Exception:
        # Several of these paths go on to parse the canned response and fail.
        # What is under test is the kwargs of the requests already made.
        pass

    assert studip.session.calls, "{} made no request".format(path)
    assert "MISSING" not in studip.session.timeouts, \
        "{} made a request without a timeout".format(path)


def test_the_whole_login_handshake_is_covered(session):
    """Three requests, not one: the login page, the credential POST, and the
    check that the session took."""
    studip = session(FakeResponse(text=LOGIN_PAGE))

    studip.login("general", {}, "user", "password")

    assert len(studip.session.calls) == 3
    assert studip.session.timeouts == [studip.timeout] * 3
