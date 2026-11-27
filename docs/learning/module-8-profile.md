# Redirect profile: /fNkSZBX, 300 requests per pass

## Latency (ms, in process: no network)

| scenario | requests | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| cache hit | 300 | 5.87 | 5.22 | 9.28 | 12.72 | 20.32 |
| cache miss | 300 | 15.29 | 14.01 | 22.31 | 29.72 | 64.58 |

## CPU (cProfile, cache-miss pass, sorted by own time)

```text
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
    24000    2.331    0.000    2.331    0.000 {built-in method _overlapped.GetQueuedCompletionStatus}
17401/17400    0.181    0.000   10.754    0.001 base_events.py:1982(_run_once)
     2700    0.162    0.000    0.162    0.000 {method 'WSASend' of '_overlapped.Overlapped' objects}
    17400    0.111    0.000    2.561    0.000 windows_events.py:765(_poll)
211200/210600    0.068    0.000    0.079    0.000 {built-in method builtins.isinstance}
25200/24000    0.064    0.000    7.808    0.000 {method 'run' of '_contextvars.Context' objects}
     9000    0.064    0.000    0.094    0.000 <frozen importlib._bootstrap_external>:101(_path_join)
7500/3900    0.061    0.000    0.476    0.000 utils.py:643(solve_dependencies)
3000/1200    0.059    0.000    0.235    0.000 {method 'switch' of 'greenlet.greenlet' objects}
    21600    0.052    0.000    0.091    0.000 base_events.py:847(_call_soon)
     6600    0.045    0.000    0.094    0.000 windows_events.py:718(_register)
     1800    0.044    0.000    0.044    0.000 {built-in method nt.stat}
```

## Memory (tracemalloc, cache-miss pass)

- Peak traced memory: 452 KiB
- Memory still held after the pass: 177.9 KiB = 607 bytes per request

```text
C:\Users\User\AppData\Local\Programs\Python\Python314\Lib\re\__init__.py:285: size=18.8 KiB (+18.8 KiB), count=409 (+409), average=47 B
C:\Users\User\Desktop\mine\work\Amalitech\url-shortener-fastapi\.venv\Lib\site-packages\httpx\_models.py:162: size=10.1 KiB (+10.1 KiB), count=216 (+216), average=48 B
C:\Users\User\Desktop\mine\work\Amalitech\url-shortener-fastapi\.venv\Lib\site-packages\httpx\_models.py:401: size=6624 B (+6624 B), count=72 (+72), average=92 B
C:\Users\User\AppData\Local\Programs\Python\Python314\Lib\asyncio\locks.py:168: size=6080 B (+6080 B), count=16 (+16), average=380 B
C:\Users\User\Desktop\mine\work\Amalitech\url-shortener-fastapi\.venv\Lib\site-packages\anyio\streams\memory.py:127: size=5859 B (+5859 B), count=93 (+93), average=63 B
C:\Users\User\Desktop\mine\work\Amalitech\url-shortener-fastapi\.venv\Lib\site-packages\anyio\_backends\_asyncio.py:419: size=5184 B (+5184 B), count=24 (+24), average=216 B
C:\Users\User\Desktop\mine\work\Amalitech\url-shortener-fastapi\.venv\Lib\site-packages\anyio\_backends\_asyncio.py:412: size=5184 B (+5184 B), count=24 (+24), average=216 B
C:\Users\User\Desktop\mine\work\Amalitech\url-shortener-fastapi\.venv\Lib\site-packages\anyio\_core\_streams.py:51: size=4410 B (+4410 B), count=70 (+70), average=63 B
```
## How to read this

- **Latency:** the cache makes a redirect about 2.7x faster (p50 5.2 ms vs 14.0 ms). Measured
  in process from Windows to PostgreSQL/Redis in Docker, so most of each miss is the
  database round trip.
- **CPU:** the top line (`GetQueuedCompletionStatus`) is the event loop WAITING for the
  network (Windows' IOCP). That is idle time, not work: while one request waits, others run.
  Our own code barely shows up; FastAPI's dependency solving is the largest real CPU cost.
- **Memory:** 300 requests left 178 KiB held, 900 requests 227 KiB (3x the requests, +27%
  memory): caches filling up (regex, httpx, AnyIO streams), not a leak. A leak grows
  with every request.
- **A profiling mistake found and fixed:** the first run replaced the click tracker with
  `app.dependency_overrides`. FastAPI then re-inspects the override on every request:
  `get_dependant` / `inspect` appeared in the CPU table and memory peaked at 1574 KiB.
  Swapping `app.state.click_tracker` instead removed it (peak 452 KiB). The measuring
  tool had changed what it measured.

## Load test (Locust, 50 users, 30 s, Gunicorn with 3 workers in Docker)

| | max_requests = 1000 | max_requests = 10,000 + control socket off |
|---|---|---|
| requests | 3709 | 3893 |
| failures | 8 (0.22%, `ConnectionResetError`) | **0** |
| requests per second | 125 | **131** |
| p50 / p95 / p99 | 33 / 240 / 710 ms | **35 / 190 / 290 ms** |
| max | 1732 ms | **621 ms** |
| worker restarts during the test | 3 | **0** |

The 8 resets happened exactly when the workers reached `max_requests` and were recycled:
an exiting worker closes its keep-alive connections, and a client reusing one at that
moment gets a reset; the fresh worker then opens new database and Redis connections,
which made the slow tail (p99). The profile showed no leak, so recycling is now a rare
safety net (10,000 + jitter). All 2664 clicks of the first run were saved by the worker
(queue empty at the end): write-behind kept up with ~90 redirects per second.
