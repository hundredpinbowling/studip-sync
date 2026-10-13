"""Writing a file so that it is either absent or complete, never half-written.

Every download this program performs used to stream straight into its final
path with `shutil.copyfileobj`, which has no notion of being interrupted. A
connection reset, a full disk, or the Ctrl-C that a 40-minute sync invites
leaves a truncated file sitting at the name the complete one would have. How
bad that is depends on the path:

- Media files are the serious case. download_media decides what to skip purely
  by the hash in the filename, with no size to check against, so a truncated
  media file is skipped by every later run: the corruption is silent and
  permanent.
- A file in the synced library is caught eventually — is_file_new compares
  size, so the next run re-downloads it — but until then the library holds a
  corrupt file under a real name, and the good previous version has already
  been renamed to .old by the code that was about to overwrite it.
- The bulk .zip of the --old path fails loudly when unzipped, costing a run
  rather than data.

Inspired by studip-sync/studip-sync#36, which introduced the same temp-file
dance for the streaming downloads in session.py. Two deliberate differences:
the temp path is recorded before any byte is written, so a failure mid-copy
cannot leak the partial file the way that PR's version does, and cleanup is
hung on BaseException, because Ctrl-C during a long sync is the single most
likely way to get here. The final copy into the synced library in
studip_rsync.py — the write that matters most to the user, and the one the PR
leaves alone — goes through replace_file below.
"""
import os
import shutil
import tempfile


def _write_atomically(path, write):
    """Call `write(file)` into a temp file, then move it onto `path`.

    The temp file is created in the destination's own directory, since
    os.replace is only atomic within a filesystem and the destination's is the
    only one guaranteed to be right. It is named ".part-*" so that an
    interrupted write that outlives its cleanup is recognisable rather than
    looking like course material.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    file = tempfile.NamedTemporaryFile("wb", dir=directory, prefix=".part-", delete=False)
    temp_path = file.name

    try:
        with file:
            write(file)
            file.flush()
            # The rename is atomic, but it says nothing about the data having
            # reached the disk: without this, a crash can leave the new name
            # pointing at an empty file.
            os.fsync(file.fileno())

        os.replace(temp_path, path)
    except BaseException:
        # BaseException, not Exception: a sync long enough to be interrupted by
        # hand is exactly the case this cleanup exists for, and KeyboardInterrupt
        # is not an Exception.
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def stream_to_file(response, path):
    """Stream a requests response body to `path`, completely or not at all."""
    _write_atomically(path, lambda file: shutil.copyfileobj(response.raw, file))


def replace_file(source, destination):
    """Copy `source` onto `destination`, completely or not at all.

    A copy rather than a move: the source is a staging file in the work
    directory, which may be on another filesystem, and the callers check its
    size against what Stud.IP promised before and after.
    """
    with open(source, "rb") as source_file:
        _write_atomically(destination, lambda file: shutil.copyfileobj(source_file, file))
