"""Run slow password hashing (Argon2id) in its own small thread pool.

Each Argon2id run takes 64 MB of memory and real CPU time on purpose. Without a
cap, a flood of register or login requests would start one thread per request
(up to 32 by default): about 2 GB per worker, and every other request slowed down.
This pool has only a few threads: extra hashes wait in its queue instead.
A pool (not an asyncio.Semaphore) because a pool is not tied to one event loop.
"""

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

# At most this many hashes run at the same time in one worker process
MAX_PARALLEL_PASSWORD_WORK = 4

_pool = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_PASSWORD_WORK, thread_name_prefix="password-work"
)

P = ParamSpec("P")
R = TypeVar("R")


async def run_password_work(function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run function(*args) in the password pool; the event loop keeps serving others."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, functools.partial(function, *args, **kwargs))
