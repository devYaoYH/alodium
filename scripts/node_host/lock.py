"""
lock — the pass lock both host jobs take, now without `stat -f || stat -c`.

The bash took `mkdir .task-dispatch/<name>.lock`: mkdir is atomic, so exactly
one pass wins, and a crashed pass's lock is stolen once it is older than a
threshold (30 min for the dispatcher, 2 h for deploy-watch, whose pass spans a
live deploy). The only unportable part was reading the directory's age.

This keeps the SAME mechanism at the SAME path on purpose, rather than moving
to flock/fcntl (POSIX-only) or msvcrt (Windows-only): os.mkdir is atomic on
every platform Python runs on, and during the cutover a bash pass still on an
old checkout and a Python pass honour each other's lock.

`decide` is the pure part — given whether the directory exists and how old it
is, what the bash would have done — and has a test per branch.
"""

import os
import time

ACQUIRED = "acquired"      # mkdir succeeded
STOLEN = "stolen"          # stale lock removed, then mkdir succeeded
HELD = "held"              # someone else's fresh lock: exit 0
CONTENDED = "contended"    # stale, but another pass re-took it first: exit 0


def is_stale(age_seconds: int, stale_after: int) -> bool:
    """`[[ $(( now - mtime )) -gt N ]]` — strictly greater, whole seconds."""
    return age_seconds > stale_after


class PassLock:
    def __init__(self, path, stale_after: int, clock=time.time):
        self.path = str(path)
        self.stale_after = stale_after
        self._clock = clock
        self.held = False

    def _mkdir(self) -> bool:
        try:
            os.mkdir(self.path)
        except FileExistsError:
            return False
        return True

    def _age(self):
        """Whole seconds since the lock's mtime, or None if it is gone/not a dir."""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        if not os.path.isdir(self.path):
            return None
        return int(self._clock()) - int(st.st_mtime)

    def acquire(self) -> str:
        if self._mkdir():
            self.held = True
            return ACQUIRED
        age = self._age()
        if age is None or not is_stale(age, self.stale_after):
            # The bash's `[[ -d "$LOCK" ]] && [[ age -gt N ]]` was false: a
            # fresh lock, or a non-directory squatting on the path (mkdir
            # failed and -d is false). Both exit 0 without touching it.
            return HELD
        try:
            os.rmdir(self.path)
        except OSError:
            pass                      # `rmdir ... || true`
        if self._mkdir():
            self.held = True
            return STOLEN
        return CONTENDED

    def release(self):
        """The EXIT trap: `rmdir "$LOCK" 2>/dev/null || true`."""
        if self.held:
            try:
                os.rmdir(self.path)
            except OSError:
                pass
            self.held = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False
