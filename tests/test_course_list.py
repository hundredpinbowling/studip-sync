"""Session.get_courses, as rerouted onto JSON:API by the fork's 4165bb0.

Covers the behaviour upstream's `MyCoursesData` scrape used to provide, plus
the paging rule that is easy to get wrong and fails silently when wrong.
"""
import pytest

ENROLMENTS = [
    ("c01", "Stochastic processes I", "WiSe 2024/25"),
    ("c02", "Stochastic processes I - exercises", "WiSe 2024/25"),
    ("c03", "Ökonometrie I/ Econometrics I", "SoSe 2025"),
    ("c04", "Probabilistic Machine Learning", "WiSe 2025/26"),
    ("c05", "Machine Learning", "SoSe 2026"),
    ("c06", "Practical Statistical Training [M.WIWI-QMW.0020]", "WiSe 2026/27"),
    ("c07", "Datenschutz und Datenethik in angewandter Statistik [M.WIWI-QMW.0038]",
     "WiSe 2026/27"),
    ("c08", "Datenbanken", "WiSe 2026/27"),
]


def test_lists_every_enrolment(jsonapi_session):
    session = jsonapi_session(ENROLMENTS)

    courses = session.get_courses()

    assert {course["course_id"] for course in courses} == {c[0] for c in ENROLMENTS}


def test_orders_by_semester_then_title(jsonapi_session):
    """A stable order, so an interrupted run resumes predictably."""
    session = jsonapi_session(ENROLMENTS)

    courses = session.get_courses()
    keys = [(course["semester_id"], course["save_as"]) for course in courses]

    assert keys == sorted(keys)


@pytest.mark.parametrize("page_cap", [7, 30, 100])
def test_pages_through_the_whole_collection(jsonapi_session, page_cap):
    """The server may cap pages below the requested limit.

    Advancing the offset by the *requested* limit instead of the number of
    items returned truncates the list with no error at all — 30 of 45
    enrolments, and a sync that looks like it succeeded.
    """
    enrolments = [("c%02d" % i, "Course %d" % i, "WiSe 2026/27") for i in range(45)]

    session = jsonapi_session(enrolments, page_cap=page_cap)
    courses = session.get_courses()

    assert len(courses) == 45


def test_semester_ids_rise_chronologically(jsonapi_session):
    """--recent and the "N - Semester" directory names both depend on this."""
    session = jsonapi_session(ENROLMENTS)

    courses = session.get_courses()
    by_semester = {course["semester"]: course["semester_id"] for course in courses}

    assert by_semester["WiSe 2024/25"] < by_semester["SoSe 2025"] < by_semester["WiSe 2025/26"]
    assert by_semester["WiSe 2025/26"] < by_semester["SoSe 2026"] < by_semester["WiSe 2026/27"]


def test_recent_means_the_semester_studip_calls_current(jsonapi_session):
    """Not simply the newest: enrolling in a future course must not hijack it."""
    future = ENROLMENTS + [("c99", "Some course next summer", "SoSe 2026")]
    session = jsonapi_session(future)

    courses = session.get_courses(only_recent_semester=True)

    assert {course["semester"] for course in courses} == {"WiSe 2026/27"}
    assert [course["save_as"] for course in courses] == [
        "Datenbanken",
        "Datenschutz und Datenethik in angewandter Statistik [M.WIWI-QMW.0038]",
        "Practical Statistical Training [M.WIWI-QMW.0020]",
    ]


def test_titles_become_safe_directory_names(jsonapi_session):
    session = jsonapi_session([("c01", "  Spaced   out /   slashed  ", "WiSe 2026/27")])

    courses = session.get_courses()

    assert courses[0]["save_as"] == "Spaced out -- slashed"


def test_courses_sharing_a_title_get_separate_directories(jsonapi_session):
    """Parallel Übung groups; a collision would interleave two courses' files."""
    session = jsonapi_session([
        ("c01", "Numerische Mathematik I - Übungen", "WiSe 2026/27"),
        ("c02", "Numerische Mathematik I - Übungen", "WiSe 2026/27"),
    ])

    courses = session.get_courses()
    names = [course["save_as"] for course in courses]

    assert len(set(names)) == 2
    assert names[0] == "Numerische Mathematik I - Übungen"
    assert names[1].startswith("Numerische Mathematik I - Übungen (")


def test_one_entry_per_course(jsonapi_session):
    session = jsonapi_session(ENROLMENTS + ENROLMENTS[:2])

    courses = session.get_courses()

    assert len(courses) == len(ENROLMENTS)


def test_unidentifiable_user_is_an_error_not_an_empty_sync(jsonapi_session):
    from studip_sync.session import SessionError

    session = jsonapi_session(ENROLMENTS, user_id=None)

    with pytest.raises(SessionError, match="did not identify the user"):
        session.get_courses()
