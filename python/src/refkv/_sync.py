"""The event loop that the methods of refkv's stores that return at once run on.

A store's operations are coroutines (`get_async`, ...); `get`, `classify`, `raw`
and `list` run them here, on one loop on a thread of its own, started on first
use. Running them there rather than in `asyncio.run` keeps one loop for the life
of a store, and works where a loop already runs on the caller's thread, as in a
notebook.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _the_loop() -> asyncio.AbstractEventLoop:
    global _loop  # noqa: PLW0603 (one loop per process, made once under the lock)
    with _lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="refkv-loop", daemon=True).start()
            _loop = loop
        return _loop


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """The result of `coro`, run on refkv's loop; the caller's thread waits for it."""
    loop = _the_loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        coro.close()
        raise RuntimeError("a refkv method that returns at once was called from a coroutine on "
                           "refkv's own event loop, which it would block; await its _async form")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()
