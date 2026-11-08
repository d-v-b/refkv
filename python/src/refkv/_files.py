"""Writing a file so that a failure keeps the one it would replace."""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO


@contextlib.contextmanager
def replacing(path: str | os.PathLike[str]) -> Generator[BinaryIO]:
    """A binary file whose contents take the place of the file at `path` when the block ends,
    so a block that raises leaves `path` as it was, and removes what it wrote.

    The file is a temporary one beside `path`, made by `open` so that the umask applies to it as
    to any new file, and closed before it is moved: Windows moves no open file. It takes the mode
    of a file it replaces, as writing that file in place would keep it, and `open` refuses what
    it would refuse in place, such as a read-only file. `path` is followed through a symlink, and
    a device or a pipe is written in place: neither is a file to replace.

    The directory must be writable, and on Windows no other process may hold `path` open: each
    raises PermissionError, with `path` as it was. A replaced file keeps no hard links, and has
    the owner of this process."""
    target = Path(path).resolve()
    try:
        previous = target.stat()
    except FileNotFoundError:
        previous = None
    if previous is not None:
        if not stat.S_ISREG(previous.st_mode):
            with open(path, "wb") as f:
                yield f
            return
        with open(target, "r+b"):
            pass
    temporary = target.parent / f".refkv-{secrets.token_hex(8)}.tmp"
    try:
        with open(temporary, "xb") as f:
            if previous is not None:
                temporary.chmod(stat.S_IMODE(previous.st_mode))
            yield f
            f.flush()
            os.fsync(f.fileno())  # its contents on disk before its name is the file's
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
