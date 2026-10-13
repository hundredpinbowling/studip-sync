"""The skip_courses config key added by 41c4759."""
import pytest


@pytest.fixture
def skip_courses(monkeypatch):
    """Set config's skip_courses for one test.

    get_config() is cached, so this patches the one instance the code under
    test reads; monkeypatch restores the key afterwards.
    """
    from studip_sync.config import get_config

    def apply(patterns):
        monkeypatch.setitem(get_config().config, "skip_courses", patterns)

    return apply


COURSES = [
    {"course_id": "aaa111", "save_as": "Practical Statistical Training [M.WIWI-QMW.0020]"},
    {"course_id": "bbb222",
     "save_as": "Forum für Studierende der Mathematik und Informatik (unbegrenzt)"},
    {"course_id": "ccc333", "save_as": "Datenbanken"},
]


def filter_courses(courses=None):
    from studip_sync.studip_rsync import filter_skipped_courses

    return filter_skipped_courses(courses if courses is not None else COURSES)


def test_no_config_key_skips_nothing(skip_courses):
    kept, skipped = filter_courses()

    assert len(kept) == 3
    assert skipped == []


def test_matches_a_title_substring(skip_courses):
    skip_courses(["Forum für Studierende"])

    kept, skipped = filter_courses()

    assert [course["course_id"] for course in skipped] == ["bbb222"]
    assert [course["course_id"] for course in kept] == ["aaa111", "ccc333"]


def test_title_match_ignores_case(skip_courses):
    skip_courses(["forum FÜR studierende"])

    _, skipped = filter_courses()

    assert [course["course_id"] for course in skipped] == ["bbb222"]


def test_matches_an_exact_course_id(skip_courses):
    skip_courses(["ccc333"])

    kept, skipped = filter_courses()

    assert [course["course_id"] for course in skipped] == ["ccc333"]
    assert len(kept) == 2


def test_a_single_string_is_accepted_like_a_list(skip_courses):
    """Easy mistake to make in hand-written JSON; "Forum" must not become
    a list of characters, which would match every course."""
    skip_courses("Datenbanken")

    kept, skipped = filter_courses()

    assert [course["course_id"] for course in skipped] == ["ccc333"]
    assert len(kept) == 2


def test_empty_patterns_are_ignored(skip_courses):
    """An empty string is a substring of everything — it must not skip all."""
    skip_courses(["", "Datenbanken"])

    kept, skipped = filter_courses()

    assert [course["course_id"] for course in skipped] == ["ccc333"]
    assert len(kept) == 2


def test_nothing_matching_leaves_the_list_alone(skip_courses):
    skip_courses(["Course that is not enrolled"])

    kept, skipped = filter_courses()

    assert len(kept) == 3
    assert skipped == []
