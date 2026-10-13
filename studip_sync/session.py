import os
import re
import time
import urllib.parse
import json

import requests

from studip_sync import parsers
from studip_sync.atomic import stream_to_file
from studip_sync.constants import URL_BASEURL_DEFAULT, AUTHENTICATION_TYPES, \
    HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT, HTTP_DOWNLOAD_READ_TIMEOUT
from studip_sync.parsers import ParserError
from studip_sync.plugins.plugin_list import PluginList


class SessionError(Exception):
    pass


class FileError(Exception):
    pass


class MissingFeatureError(Exception):
    pass


class MissingPermissionFolderError(Exception):
    pass


class DownloadError(SessionError):
    pass


class SessionExpiredError(DownloadError):
    """Stud.IP no longer recognises the session, mid-run.

    A DownloadError on purpose: the --old bulk path in studip_sync.py has no
    notion of a session dying and need not grow one — its DownloadError
    handler records the failed course and moves on, which is the best it can
    do. The callers that can do better — retry_once_after_relogin, and
    StudIPRSync.sync, which stops the run on a message — catch this class
    ahead of DownloadError.
    """


# An anonymous request is redirected here, whatever it asked for. The landing
# URL is the only dependable signal: Stud.IP serves the login page with HTTP
# 200, so the status code says nothing, and the page renders in German or
# English depending on account state, so the body cannot be matched either.
# The login check in logins/general.py uses the same signal.
LOGIN_PATH = "/dispatch.php/login"


def raise_if_logged_out(response):
    """Raise SessionExpiredError if a request landed on the login page.

    Stud.IP drops a session mid-run — an absolute server-side cap, or the
    instance shedding sessions under a burst of requests, which is the same
    flakiness that makes requests worth retrying at all. Every subsequent
    request then answers with the login page rather than an error, so without
    this check that page reaches the HTML parsers as if it were course
    content, and the run dies on a bare AttributeError.
    """
    if LOGIN_PATH in response.url:
        raise SessionExpiredError(
            "Stud.IP session expired: the request landed on {}".format(response.url))


def semester_sort_key(semester):
    """Order semesters by start date, tolerating ints or ISO strings."""
    start = semester.get("attributes", {}).get("start")

    try:
        return (0, float(start), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(start or ""))


class URL(object):
    def __init__(self, base_url):
        self.base_url = base_url

    def __relative_url(self, rel_url):
        return urllib.parse.urljoin(self.base_url, rel_url)

    def login_page(self):
        return self.__relative_url("")

    def files_main(self):
        return self.__relative_url("dispatch.php/course/files")

    def files_index(self, folder_id):
        return self.__relative_url("dispatch.php/course/files/index/{}".format(folder_id))

    def files_flat(self):
        return self.__relative_url("dispatch.php/course/files/flat")

    def bulk_download(self, folder_id):
        return self.__relative_url("dispatch.php/file/bulk/{}".format(folder_id))

    def studip_main(self):
        return self.__relative_url("dispatch.php/start")

    def courses(self):
        return self.__relative_url("dispatch.php/my_courses")

    def mediacast_list(self):
        return self.__relative_url("plugins.php/mediacastplugin/media/index")

    def files_api_top_folder(self, course_id):
        return self.__relative_url("api.php/course/{}/top_folder".format(course_id))

    def files_api_folder(self, folder_id):
        return self.__relative_url("api.php/folder/{}".format(folder_id))

    def files_api_download(self, file_id):
        return self.__relative_url("api.php/file/{}/download".format(file_id))

    def jsonapi(self, path):
        return self.__relative_url("jsonapi.php/v1/{}".format(path))


class Session(object):

    # Class attributes so that a Session is never without them, however it was
    # built — __init__ overrides them per instance.
    timeout = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
    download_timeout = (HTTP_CONNECT_TIMEOUT, HTTP_DOWNLOAD_READ_TIMEOUT)

    def __init__(self, plugins=None, base_url=URL_BASEURL_DEFAULT,
                 connect_timeout=HTTP_CONNECT_TIMEOUT, read_timeout=HTTP_READ_TIMEOUT,
                 download_read_timeout=HTTP_DOWNLOAD_READ_TIMEOUT):
        super(Session, self).__init__()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "WeWantFileSync"})
        self.url = URL(base_url)
        self.timeout = (connect_timeout, read_timeout)
        self.download_timeout = (connect_timeout, download_read_timeout)

        if plugins is None:
            self.plugins = PluginList()
        else:
            self.plugins = plugins

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.session.__exit__()

    def _timeout_for(self, kwargs):
        """The timeout a request gets, unless it asked for one itself.

        Keyed on `stream`, which marks exactly the requests whose first byte
        is slow — Stud.IP builds a bulk .zip before it answers — and no
        others. Doing it here rather than at the call sites keeps every
        request covered: a new one has to opt out to go without a timeout,
        instead of silently going without until someone notices.
        """
        if "timeout" in kwargs:
            return kwargs.pop("timeout")

        return self.download_timeout if kwargs.get("stream") else self.timeout

    def get(self, url, **kwargs):
        return self.session.get(url, timeout=self._timeout_for(kwargs), **kwargs)

    def post(self, url, **kwargs):
        return self.session.post(url, timeout=self._timeout_for(kwargs), **kwargs)

    def set_base_url(self, new_base_url):
        self.url = URL(new_base_url)

    def login(self, auth_type, auth_type_data, username, password):
        auth = AUTHENTICATION_TYPES[auth_type]
        auth.login(self, username, password, auth_type_data)

    JSONAPI_PAGE_LIMIT = 100

    def jsonapi_get(self, path, **params):
        with self.get(self.url.jsonapi(path), params=params or None) as response:
            if not response.ok:
                raise SessionError("JSON:API request failed: {} -> {}".format(
                    path, response.status_code))
            try:
                return response.json()
            except ValueError:
                raise SessionError("JSON:API answered non-JSON for " + path)

    def jsonapi_collection(self, path, **params):
        """Walk every page of a JSON:API collection.

        Returns (data, included). Collections default to 30 per page and cap
        the limit, so paging is not optional: 45 memberships already exceed it.
        """
        data = []
        included = []

        while True:
            page = self.jsonapi_get(path, **dict(params, **{
                "page[offset]": len(data), "page[limit]": self.JSONAPI_PAGE_LIMIT}))
            items = page.get("data") or []
            data += items
            included += page.get("included") or []

            # Advance by what the server returned, not by what was asked for:
            # it may cap the page size below JSONAPI_PAGE_LIMIT, and counting
            # in requested units then skips the rest of the collection.
            total = page.get("meta", {}).get("page", {}).get("total", len(data))
            if not items or len(data) >= total:
                return data, included

    def get_courses(self, only_recent_semester=False):
        """The courses the user is a member of, newest semester last.

        Not parsed out of the my_courses page as upstream does: Göttingen runs
        Stud.IP 6.2, which ships that page's Vue app with empty props and
        fetches the list client-side, so the `MyCoursesData = {...}` assignment
        upstream greps for does not exist. It also answers 404 for every
        api.php route. The JSON:API is what this instance actually maintains.
        """
        me = self.jsonapi_get("users/me").get("data", {}).get("id")
        if not me:
            raise SessionError("Failed to get courses: JSON:API did not identify the user")

        memberships, included = self.jsonapi_collection(
            "users/{}/course-memberships".format(me), include="course,course.start-semester")

        courses = {item["id"]: item for item in included if item.get("type") == "courses"}
        semesters = {item["id"]: item for item in included if item.get("type") == "semesters"}

        # Chronological, so semester_id rises with time and the newest semester
        # holds the highest one — the ordering --recent and the "N - Semester"
        # directory names of use_new_file_structure both rely on.
        order = sorted(semesters, key=lambda sem_id: semester_sort_key(semesters[sem_id]))
        semester_ids = {sem_id: i + 1 for i, sem_id in enumerate(order)}

        # Prefer the semester Stud.IP itself flags as current: "latest" would
        # mean a future semester as soon as one course is registered in it.
        recent = next((sem_id for sem_id in order
                       if semesters[sem_id].get("attributes", {}).get("is-current")),
                      order[-1] if order else None)

        listed = []
        for membership in memberships:
            course_id = (membership.get("relationships", {}).get("course", {})
                         .get("data") or {}).get("id")
            course = courses.get(course_id)
            if not course:
                continue

            sem_id = (course.get("relationships", {}).get("start-semester", {})
                      .get("data") or {}).get("id")

            if only_recent_semester and sem_id != recent:
                continue

            save_as = course.get("attributes", {}).get("title", "").strip()
            save_as = re.sub(r"\s\s+", " ", save_as).replace("/", "--")

            listed.append({
                "course_id": course_id,
                "save_as": save_as,
                "semester": semesters.get(sem_id, {}).get("attributes", {}).get(
                    "title", "unknown semester").strip(),
                "semester_id": semester_ids.get(sem_id, 0)
            })

        # One membership per course, and a stable order to sync in.
        unique = {course["course_id"]: course for course in listed}
        courses = sorted(unique.values(), key=lambda course: (course["semester_id"],
                                                              course["save_as"],
                                                              course["course_id"]))

        # Distinct courses can carry the same title — parallel Übung groups
        # especially. save_as becomes a directory name, so collisions would
        # interleave two courses' files; keep them apart.
        seen = {}
        for course in courses:
            key = (course["semester_id"], course["save_as"])
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                course["save_as"] = "{} ({})".format(course["save_as"],
                                                     course["course_id"][:8])

        return courses

    def check_course_new_files(self, course_id, last_sync):
        params = {"cid": course_id}

        with self.get(self.url.files_flat(), params=params) as response:
            raise_if_logged_out(response)
            if not response.ok:
                if response.status_code == 403 and "Documents" in response.text:
                    raise MissingFeatureError("This course has no files")
                else:
                    raise DownloadError("Cannot access course files_flat page")
            last_edit = parsers.extract_files_flat_last_edit(response.text)

        if last_edit == 0:
            print("\tLast file edit couldn't be detected!")
        else:
            print("\tLast file edit: {}".format(
                time.strftime("%d.%m.%Y %H:%M", time.gmtime(last_edit))))
        return last_edit == 0 or last_edit > last_sync

    def download(self, course_id, workdir, sync_only=None):
        params = {"cid": course_id}

        with self.get(self.url.files_main(), params=params) as response:
            raise_if_logged_out(response)
            if not response.ok:
                raise DownloadError("Cannot access course files page")
            folder_id = parsers.extract_parent_folder_id(response.text)
            csrf_token = parsers.extract_csrf_token(response.text)

        download_url = self.url.bulk_download(folder_id)
        data = {
            "security_token": csrf_token,
            # "parent_folder_id": folder_id,
            "ids[]": sync_only or folder_id,
            "download": 1
        }

        with self.post(download_url, params=params, data=data, stream=True) as response:
            raise_if_logged_out(response)
            if not response.ok:
                raise DownloadError("Cannot download course files")
            path = os.path.join(workdir, course_id)
            stream_to_file(response, path)
            return path

    def download_file(self, download_url, tempfile):
        with self.post(download_url, stream=True) as response:
            raise_if_logged_out(response)
            if not response.ok:
                raise DownloadError("Cannot download file")

            stream_to_file(response, tempfile)


    def download_file_api(self, file_id, tempfile):
        download_url = self.url.files_api_download(file_id)

        with self.get(download_url, stream=True) as response:
            raise_if_logged_out(response)
            if not response.ok:
                print(response.text)
                raise DownloadError("Cannot download file")

            stream_to_file(response, tempfile)

    def get_files_index(self, course_id, folder_id=None):
        params = {"cid": course_id}

        if folder_id:
            url = self.url.files_index(folder_id)
        else:
            url = self.url.files_main()

        with self.get(url, params=params) as response:
            raise_if_logged_out(response)
            if not response.ok:
                if response.status_code == 403 and "Documents" in response.text:
                    raise MissingFeatureError("This course has no files")
                elif response.status_code == 403 and "Zugriff verweigert" in response.text:
                    raise MissingPermissionFolderError(
                        "You are missing the required pemissions to view this folder")
                else:
                    raise DownloadError("Cannot access course files/files_index page")
            return parsers.extract_files_index_data(response.text)

    def get_files_index_from_api(self, course_id, folder_id=None):
        if folder_id:
            url = self.url.files_api_folder(folder_id)
        else:
            url = self.url.files_api_top_folder(course_id)

        with self.get(url) as response:
            # The api.php routes answer JSON, but an expired session is still
            # redirected to the login page — with HTTP 200, so without this
            # the whole page would be printed by the branch below or, worse,
            # fed to json.loads.
            raise_if_logged_out(response)
            if not response.ok:
                print(response.text)
                raise DownloadError("Cannot access course files/files_index page")

            res = json.loads(response.text)
            
            return res["file_refs"], res["subfolders"]

    def download_media(self, course_id, media_workdir, course_save_as):
        params = {"cid": course_id}

        mediacast_list_url = self.url.mediacast_list()

        with self.get(mediacast_list_url, params=params) as response:
            raise_if_logged_out(response)
            if not response.ok:
                if response.status_code == 500 and "not found" in response.text:
                    raise MissingFeatureError("This course has no media")
                else:
                    raise DownloadError("Cannot access mediacast list page")

            media_files = parsers.extract_media_list(response.text)

        os.makedirs(media_workdir, exist_ok=True)

        workdir_files = os.listdir(media_workdir)

        print("\tFound {} media files".format(len(media_files)))

        for media_file in media_files:
            media_hash = media_file["hash"]
            media_type = media_file["type"]
            media_player_url_relative = media_file["media_url"]
            media_player_url = urllib.parse.urljoin(mediacast_list_url,
                                                       media_player_url_relative)

            # files are saved as "{filename}-{hash}.{extension}"
            # older version might have used the format "{hash}-{filename}.{extension}"

            found_existing_file = False

            for workdir_filename in workdir_files:
                workdir_filename_split = workdir_filename.split("-")

                if workdir_filename_split[0] == media_hash or \
                        workdir_filename_split[-1].split(".")[0] == media_hash:
                    found_existing_file = True
                    break

            # Skip this file if it already exists
            if found_existing_file:
                continue

            print("\t\tDownloading " + media_hash)

            if media_type == "player":
                with self.get(media_player_url) as response:
                    raise_if_logged_out(response)
                    if not response.ok:
                        raise DownloadError("Cannot access media file page: " + media_hash)

                    download_media_url_relative = parsers.extract_media_best_download_link(
                        response.text)

                    download_media_url = urllib.parse.urljoin(media_player_url,
                                                                 download_media_url_relative)
            elif media_type == "direct_download":
                download_media_url = media_player_url
            else:
                raise ParserError("media_type is not a valid type")

            with self.get(download_media_url, stream=True) as response:
                # Ahead of the skip below: an expired session would otherwise
                # make every remaining media file "cannot download" and the
                # course would look merely incomplete.
                raise_if_logged_out(response)
                if not response.ok:
                    print("\t\tCannot download media file: " + str(response))
                    continue

                media_filename = parsers.extract_filename_from_headers(response.headers)

                media_filename_split = media_filename.split(".")
                media_filename_extension = media_filename_split.pop()
                media_filename_name = ".".join(media_filename_split)

                filename = media_filename_name + "-" + media_hash + "." + media_filename_extension

                filepath = os.path.join(media_workdir, filename)

                if os.path.exists(filepath):
                    raise FileError(
                        "Cannot access filepath since file already exists: " + filepath)

                # Was an open()/copyfileobj straight onto filepath, with an
                # `except OSError: os.remove(filepath)` that could not work:
                # if the open itself failed there was nothing to remove, and
                # os.remove then raised FileNotFoundError over the real error.
                stream_to_file(response, filepath)

                self.plugins.hook("hook_file_download_successful", media_filename, course_save_as,
                                  filepath)
