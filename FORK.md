# The `goettingen` fork

This branch makes studip-sync work against the University of Göttingen's
Stud.IP, which runs **6.2**. Upstream release 2.1.1 predates it in three
independent ways:

- **Login.** The submit control, the target form, and the "did it work?" check
  are all different, and the failure was silent: correct credentials reported as
  wrong.
- **Course list.** The `my_courses` page ships its Vue app with empty props and
  fetches the list client-side, so there is nothing to scrape. The list comes
  from JSON:API instead.
- **`api.php`.** Every classic route 404s on this instance, so `--disable-api` is
  mandatory rather than a preference. Folder discovery and downloads go through
  the HTML fallback.

## The commits, and why each exists

```sh
git log --oneline upstream/master..goettingen    # the list
git log upstream/master..goettingen              # with the reasoning
```

Each commit message carries its own rationale: the defect, what was observed,
why the fix is shaped the way it is, and what was deliberately not done. Read
those rather than a summary kept somewhere else, which is how the copy in the
parent project's `scripts/README.md` came to be five commits out of date.

## Sending these upstream

`1e4c465` (login), `4165bb0` (course list), `9e381f7` (session dropped mid-sync)
and `fce0e4f` (path traversal in server-chosen names) are defects against current
Stud.IP rather than Göttingen-specific preferences, so they belong upstream. One
pull request per commit will land faster than the branch as a whole.

`5e010b6` (atomic downloads) and `a258f6d` (request timeouts) are taken from
[studip-sync/studip-sync#36](https://github.com/studip-sync/studip-sync/pull/36),
an open PR bundling six unrelated features across 23 files. The rest of that PR
is not worth taking: it doubles down on the `my_courses` HTML scrape that 6.2
broke, and its "don't raise on download errors" refactor would turn a dropped
session into every remaining course failing one by one. Both commits note their
deliberate differences from it.

## Tests

`tests/` holds the suite, beside the code it covers. It needs no network: the
Stud.IP responses are faked, and `tests/conftest.py` supplies the config and
`sys.path` entry that importing `studip_sync` requires.

Running it needs a virtualenv with pytest, which this checkout does not define.
Run it from the project that vendors this one, which does:

```sh
uv run pytest        # from the parent project's root
```

## Rebasing on upstream

```sh
git fetch upstream && git rebase upstream/master
```

A conflict usually means upstream touched the same code, so read their change
first: it may be the same fix, in which case drop ours. This file is new, so it
never conflicts.
