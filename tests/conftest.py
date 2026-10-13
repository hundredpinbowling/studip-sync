"""Make studip-sync importable and configured for tests.

Two hazards force the order of what follows. `studip_sync.arg_parser` parses
`sys.argv` when imported, which would choke on pytest's own arguments, and
`Config` reads a config file that has to exist — lazily now, but any test that
reads a config value needs one. Both are satisfied here, at conftest import
time, rather than in a fixture, so no test import order can get ahead of them.

The path insert is for pytest's part: it puts a test file's own directory on
`sys.path`, not the repository root, so `import studip_sync` would otherwise
only resolve by accident — when the working directory happens to be the
checkout, or when a parent project that vendors this one arranges it.
"""
import atexit
import json
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_config = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({
    "base_url": "https://studip.uni-goettingen.de",
    "auth_type": "general",
    "user": {"login": "test", "password": "test"},
    "files_destination": tempfile.mkdtemp(prefix="studip-sync-test"),
}, _config)
_config.close()
atexit.register(lambda: pathlib.Path(_config.name).unlink(missing_ok=True))

sys.argv = [sys.argv[0], "-c", _config.name]


# The five semesters of a real account, with plausible unix start dates.
SEMESTERS = {
    "WiSe 2024/25": ("sem1", 1727733600, False),
    "SoSe 2025": ("sem2", 1743458400, False),
    "WiSe 2025/26": ("sem3", 1759269600, False),
    "SoSe 2026": ("sem4", 1774994400, False),
    "WiSe 2026/27": ("sem5", 1790805600, True),
}


@pytest.fixture
def jsonapi_session():
    """A Session answering JSON:API like Göttingen's Stud.IP 6.2 does.

    `courses` is a list of (course_id, title, semester_title). `page_cap` is
    the largest page the fake server will serve regardless of the requested
    limit, which is how the real one behaves for some collections.
    """
    from studip_sync.session import Session, URL

    def build(courses, page_cap=30, user_id="user-id"):
        class FakeHTTP:
            def __init__(self):
                self.requests = []

            def get(self, url, params=None, **kwargs):
                self.requests.append((url, dict(params or {})))
                return FakeResponse(url, params or {})

        class FakeResponse:
            ok = True

            def __init__(self, url, params):
                self.url = url
                self.params = params

            def json(self):
                if self.url.endswith("/users/me"):
                    return {"data": {"type": "users", "id": user_id}} if user_id else {"data": {}}

                offset = int(self.params["page[offset]"])
                limit = min(int(self.params["page[limit]"]), page_cap)
                page = courses[offset:offset + limit]

                return {
                    "data": [{"type": "course-memberships", "id": "{}_{}".format(cid, user_id),
                              "relationships": {"course": {"data": {"type": "courses",
                                                                    "id": cid}}}}
                             for cid, _, _ in page],
                    "included":
                        [{"type": "courses", "id": cid, "attributes": {"title": title},
                          "relationships": {"start-semester": {
                              "data": {"type": "semesters", "id": SEMESTERS[semester][0]}}}}
                         for cid, title, semester in page]
                        + [{"type": "semesters", "id": SEMESTERS[semester][0],
                            "attributes": {"title": semester,
                                           "start": SEMESTERS[semester][1],
                                           "is-current": SEMESTERS[semester][2]}}
                           for _, _, semester in page],
                    "meta": {"page": {"offset": offset, "limit": limit, "total": len(courses)}},
                }

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        session = Session.__new__(Session)
        session.session = FakeHTTP()
        session.url = URL("https://studip.uni-goettingen.de")
        return session

    return build
