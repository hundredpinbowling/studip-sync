from datetime import datetime
import os
import shutil
import tempfile
import time
import unicodedata
import string

from studip_sync.arg_parser import ARGS
from studip_sync.atomic import replace_file
from studip_sync.config import get_config
from studip_sync.logins import LoginError
from studip_sync.plugins.plugins import start_plugins
from studip_sync.session import Session, DownloadError, MissingFeatureError, \
    MissingPermissionFolderError, SessionExpiredError
from studip_sync.parsers import ParserError


class StudIPRSync(object):

    def __init__(self):
        super(StudIPRSync, self).__init__()
        self.workdir = tempfile.mkdtemp(prefix="studip-sync")
        config = get_config()
        self.files_destination_dir = config.files_destination
        self.media_destination_dir = config.media_destination

        if self.files_destination_dir:
            os.makedirs(self.files_destination_dir, exist_ok=True)
        if self.media_destination_dir:
            os.makedirs(self.media_destination_dir, exist_ok=True)

    def sync(self, sync_fully=False, sync_recent=False, use_api=True):
        config = get_config()

        with Session(base_url=config.base_url, plugins=start_plugins()) as session:
            print("Logging in...")
            try:
                session.login(config.auth_type, config.auth_type_data, config.username,
                              config.password)
            except (LoginError, ParserError) as e:
                print("Login failed!")
                print(e)
                return 1

            print("Downloading course list...")

            try:
                courses = list(session.get_courses(sync_recent))
            except (LoginError, ParserError) as e:
                print("Downloading course list failed!")
                print(e)
                return 1

            if sync_recent:
                print("Syncing only the most recent semester!")

            courses, skipped = filter_skipped_courses(courses)
            for course in skipped:
                print("Skipping {} (skip_courses)".format(course["save_as"]))

            status_code = 0
            for i in range(0, len(courses)):
                course = courses[i]
                print("{}) {}: {}".format(i + 1, course["semester"], course["save_as"]))

                course_save_as = get_course_save_as(course)

                if self.files_destination_dir:
                    try:
                        files_root_dir = os.path.join(self.files_destination_dir, course_save_as)

                        retry_once_after_relogin(session, lambda: CourseRSync(
                            session, self.workdir, files_root_dir, course, sync_fully,
                            use_api).download())
                    except MissingFeatureError:
                        # Ignore if there are no files
                        pass
                    except SessionExpiredError as e:
                        return report_unrecoverable_expiry(e)
                    except DownloadError as e:
                        print("\tDownload of files failed: " + str(e))
                        status_code = 2
                        raise e

                if self.media_destination_dir:
                    try:
                        print("\tSyncing media files...")

                        media_root_dir = os.path.join(self.media_destination_dir,
                                                      course_save_as)

                        retry_once_after_relogin(session, lambda: session.download_media(
                            course["course_id"], media_root_dir, course["save_as"]))
                    except MissingFeatureError:
                        # Ignore if there is no media
                        pass
                    except SessionExpiredError as e:
                        return report_unrecoverable_expiry(e)
                    except DownloadError as e:
                        print("\tDownload of media failed: " + str(e))
                        status_code = 2
                        raise e
                    except ParserError as e:
                        print("\tDownload of media failed: " + str(e))
                        if status_code != 0:
                            raise e
                        else:
                            status_code = 2

        if self.files_destination_dir and status_code == 0:
            config.update_last_sync(int(time.time()))

        return status_code

    def cleanup(self):
        shutil.rmtree(self.workdir)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()


UNICODE_NORMALIZE_MODE = "NFKC"


def report_unrecoverable_expiry(error):
    """Say that the run is over because the session cannot be renewed.

    Reached only after retry_once_after_relogin has already tried a fresh
    login, so no later course would fare better either. Returns the status
    code to end the sync on — the point of stopping here is to end on a
    sentence rather than on the traceback this whole class exists to avoid.
    """
    print("\t" + str(error))
    print("Stopping. Re-run to resume: files already downloaded are kept and the last-sync "
          "timestamp has not moved.")

    return 2


def retry_once_after_relogin(session, action):
    """Run `action`, logging in again and retrying it if the session died.

    Stud.IP drops sessions mid-run: a long sync gets the login page back for a
    course in the middle of the list, through no fault of that course.
    Renewing the session costs a login request, where giving up costs every
    course after this one. The credentials are cached in the config after the
    first login, so this does not prompt again.

    Deliberately a single attempt: a session that dies again straight after a
    fresh login is not the transient drop this handles, and the
    SessionExpiredError is left to the caller.
    """
    try:
        return action()
    except SessionExpiredError:
        print("\tStud.IP session expired, logging in again...")

        config = get_config()

        try:
            session.login(config.auth_type, config.auth_type_data, config.username,
                          config.password)
        except (LoginError, ParserError) as e:
            raise SessionExpiredError("Stud.IP session expired, re-login failed: {}".format(e))

        return action()


def filter_skipped_courses(courses):
    """Split a course list into (kept, skipped) per config's skip_courses.

    A pattern matches a course if it equals the course id, or appears
    anywhere in the course title, compared case-insensitively. Ids are the
    precise form; titles are the convenient one, and survive a course being
    renamed only in the parts that are quoted.
    """
    patterns = get_config().skip_courses

    if not patterns:
        return courses, []

    kept = []
    skipped = []

    for course in courses:
        matched = any(pattern == course["course_id"]
                      or pattern.lower() in course["save_as"].lower()
                      for pattern in patterns)
        (skipped if matched else kept).append(course)

    return kept, skipped


def check_and_cleanup_form_data(form_data_files, form_data_folders, use_api):
    form_data_files_new = []
    for form_data in form_data_files:
        try:
            if "id" not in form_data:
                log("Skipped file that can't be downloaded: {}".format(form_data["name"]))
                continue
                
            form_id = form_data["id"]
            
            if not all(c in string.hexdigits for c in form_id):
                raise ParserError("id is not hexadecimal")

            # TODO: support links by saving them as .url files
            if "size" not in form_data or form_data["size"] is None or ("storage" in form_data and form_data["storage"] == "url") or ("icon" in form_data and form_data["icon"] == "link-extern"):
                if ARGS.v:
                    print("[Debug] " + str(form_data))
                log("Found unsupported file: {}".format(form_data["name"]))
                continue

            if use_api and "is_downloadable" in form_data and not form_data["is_downloadable"]:
                log("Skipped file that can't be downloaded: {}".format(form_data["name"]))
                continue

            new_file_data = {
                "name": unicodedata.normalize(UNICODE_NORMALIZE_MODE, form_data["name"]).replace("/", "--"),
                "id": form_id,
                "size": int(form_data["size"]),
                "chdate": int(form_data["chdate"])
            }

            if not use_api:
                new_file_data["download_url"] = form_data["download_url"]

            form_data_files_new.append(new_file_data)
        except Exception as e:
            print(form_data)
            raise ParserError("File attributes are invalid: {}".format(e))

    form_data_folders_new = []
    for form_data in form_data_folders:
        try:
            if "id" not in form_data:
                log("Skipped folder that can't be downloaded")
                continue
            form_id = form_data["id"]
            if not all(c in string.hexdigits for c in form_id):
                raise ValueError("id is not hexadecimal")

            form_data_folders_new.append({
                "name": unicodedata.normalize(UNICODE_NORMALIZE_MODE, form_data["name"]).replace("/", "--"),
                "id": form_id
            })
        except Exception as e:
            print(form_data)
            raise ParserError("Folder attributes are invalid: {}".format(e))

    return form_data_files_new, form_data_folders_new


def log(message, flush=False):
    if flush:
        print("\t\t" + message, end="\r", flush=True)
    else:
        print("\t\t" + message)


def is_file_new(file, file_path):
    if not file["size"]:
        # If there is no size, skip this file, since it cant be downloaded
        return False


    if not os.path.exists(file_path):
        log("File changed: new: {}".format(file_path))
        return True

    file_time = int(os.path.getmtime(file_path))

    chdate = file["chdate"]
    if chdate > file_time:
        log("File changed: time: {} - {} : {}".format(chdate, file_time, file_path))
        return True

    file_size = os.path.getsize(file_path)

    size = file["size"]
    if not size == file_size:
        log("File changed: size: {} - {} : {}".format(size, file_size, file_path))
        return True

    return False


def get_course_save_as(course):
    if get_config().use_new_file_structure:
        save_as_semester = course["semester"].replace("/", "--")
        save_as_semester = "{} - {}".format(course["semester_id"], save_as_semester)

        return os.path.join(save_as_semester, course["save_as"])
    else:
        return course["save_as"]


class CourseRSync:

    def __init__(self, session, workdir, root_folder, course, sync_fully, use_api):
        self.session = session
        self.workdir = workdir
        self.course_id = course["course_id"]
        self.course_save_as = course["save_as"]
        self.root_folder = root_folder
        self.sync_fully = sync_fully
        self.use_api = use_api

    def download(self):
        if self.course_has_new_files(self.sync_fully):
            print("\tSyncing files...")
            self.download_recursive()
        else:
            print("\tSkipping this course...")

    def course_has_new_files(self, sync_fully=False):
        if sync_fully:
            return True

        return self.session.check_course_new_files(self.course_id, get_config().last_sync)

    def download_recursive(self, folder_id=None, folder_path_relative=""):
        try:
            if self.use_api:
                form_data_files, form_data_folders = self.session.get_files_index_from_api(self.course_id,
                                                                              folder_id)
            else:
                form_data_files, form_data_folders = self.session.get_files_index(self.course_id,
                                                                              folder_id)
        except MissingPermissionFolderError:
            log("Couldn't view the following folder because of missing permissions: " + folder_path_relative)
            return

        form_data_files, form_data_folders = check_and_cleanup_form_data(form_data_files,
                                                                         form_data_folders, self.use_api)

        for file_data in form_data_files:
            folder_absolute = os.path.join(self.root_folder, folder_path_relative)
            file_path = os.path.join(folder_absolute, file_data["name"])
            if is_file_new(file_data, file_path):
                log("Downloading: {}: {}".format(file_data["id"], file_data["name"]))

                target_file = os.path.join(self.workdir, file_data["id"])

                if not self.use_api:
                    self.session.download_file(file_data["download_url"], target_file)
                else:
                    self.session.download_file_api(file_data["id"], target_file)

                file_size = int(file_data["size"])
                target_file_size = os.path.getsize(target_file)
                if target_file_size != file_size:
                    if ARGS.v:
                        print("[Debug] " + str(form_data_files))
                    raise DownloadError("File size didn't match expected file size: " + file_path)

                file_path_base, file_path_name = os.path.split(file_path)
                if os.path.exists(file_path):
                    timestr = datetime.strftime(datetime.now(), "%Y-%m-%d_%H+%M+%S")
                    suffix = "_" + timestr + ".old"
                    new_file_path = os.path.join(file_path_base, file_path_name + suffix)
                    os.rename(file_path, new_file_path)
                else:
                    os.makedirs(file_path_base, exist_ok=True)

                if os.path.exists(file_path):
                    raise DownloadError("File exists already, even after moving it away: " +
                                        file_path)

                # The write the user actually keeps. shutil.copyfile straight
                # onto file_path left a truncated file under the real name if
                # it was interrupted, and the size check above has already
                # happened by then — is_file_new would re-download it on the
                # next run, but only after the branch above has renamed the
                # good previous version away to .old, so until then the
                # library holds nothing but the broken copy.
                replace_file(target_file, file_path)

                self.session.plugins.hook("hook_file_download_successful", file_data["name"],
                                          self.course_save_as, file_path)

        for folder_data in form_data_folders:
            new_folder_path_relative = os.path.join(folder_path_relative, folder_data["name"])

            # self.log("Accessing folder: " + folder_data["id"] + ": " + folder_data["name"])
            self.download_recursive(folder_data["id"], new_folder_path_relative)
