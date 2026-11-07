"""Token-aware sliding-window rate limiter backed by an on-disk SQLite database.

Model: a *sliding window log*. Each admitted request writes one row
``(key_hash, ts_ms, tokens)``; a key's usage is the sum of ``tokens`` over rows with
``ts_ms > now - window``. That is exact (no fixed-window burst at the boundary).

Tokens are not known before the model runs, so a request goes through three steps:

  reserve(estimate)   atomically check the window and write the estimate
  settle(actual)      replace the estimate with the provider's reported usage
  release()           delete the reservation if the request produced nothing

``reserve`` is one ``BEGIN IMMEDIATE`` transaction (evict, sum, compare, insert), so
concurrent requests, and even concurrent gateway processes sharing the file, can never
both slip under the limit. All SQLite work runs on one dedicated thread, which keeps the
event loop free and serializes in-process access.

Eviction: expired rows are deleted for a key whenever that key is checked, and by a
periodic ``purge`` for idle keys. Correctness never depends on the purge: the sum only
ever counts rows inside the window.

Keys are stored as SHA-256 hashes, never raw.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from errors import GatewayError, rate_limit_exceeded, request_too_large

DEFAULT_LIMIT = 50_000          # tokens per window per API key
DEFAULT_WINDOW_MS = 60_000      # one minute
CHARS_PER_TOKEN = 4             # rough heuristic used only for the up-front estimate


def wall_clock_ms() -> int:
    # Wall clock, not monotonic: usage must stay meaningful across restarts.
    return time.time_ns() // 1_000_000


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _text_tokens(text: str) -> float:
    """~4 ASCII characters per token, but one token per non-ASCII character: tokenizers split
    accented, CJK and emoji text far more finely, so chars/4 would badly underestimate it."""
    if text.isascii():
        return len(text) / CHARS_PER_TOKEN
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return (len(text) - non_ascii) / CHARS_PER_TOKEN + non_ascii


def estimate_tokens(payload: dict[str, Any], default_max_tokens: int) -> int:
    """Upper-bound-ish estimate: prompt (see ``_text_tokens``) plus the completion cap."""
    tokens = 0.0
    for message in payload.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            tokens += _text_tokens(content)
        elif isinstance(content, list):  # multi-part content: count the text parts
            tokens += sum(_text_tokens(p["text"]) for p in content if isinstance(p, dict)
                          and isinstance(p.get("text"), str))
        tokens += 16 / CHARS_PER_TOKEN  # role and framing overhead
    cap = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        cap = default_max_tokens
    return math.ceil(tokens) + cap


@dataclass(frozen=True)
class Reservation:
    id: int
    key_hash: str
    tokens: int
    used: int  # window usage including this reservation, at reserve time


class UsageStore:
    """The SQLite side: every method runs on the single database thread."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="usage-db")
        self._conn: sqlite3.Connection | None = None

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def open(self) -> None:
        await self._run(self._open)

    def _open(self) -> None:
        conn = sqlite3.connect(self._path, isolation_level=None)  # explicit transactions
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("CREATE TABLE IF NOT EXISTS usage ("
                     "id INTEGER PRIMARY KEY, key_hash TEXT NOT NULL, "
                     "ts_ms INTEGER NOT NULL, tokens INTEGER NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS usage_key_ts ON usage(key_hash, ts_ms)")
        self._conn = conn

    async def close(self) -> None:
        def _close() -> None:
            if self._conn:
                self._conn.close()
        await self._run(_close)
        self._pool.shutdown(wait=True)

    # -- every operation below is a single transaction ---------------------------------

    def _tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._conn
        assert conn is not None, "UsageStore.open() was not called"
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    async def reserve(self, key_hash: str, tokens: int, limit: int, window_ms: int, now: int):
        """Return ``(row_id, used_after, 0)`` if admitted, else ``(None, used, retry_after_ms)``."""
        def work(c: sqlite3.Connection):
            c.execute("DELETE FROM usage WHERE key_hash=? AND ts_ms<=?", (key_hash, now - window_ms))
            used = c.execute("SELECT COALESCE(SUM(tokens),0) FROM usage WHERE key_hash=?",
                             (key_hash,)).fetchone()[0]
            if used + tokens > limit:
                need, freed, retry = used + tokens - limit, 0, window_ms
                for ts, t in c.execute("SELECT ts_ms, tokens FROM usage WHERE key_hash=? ORDER BY ts_ms",
                                       (key_hash,)):
                    freed += t
                    if freed >= need:  # the soonest moment enough old tokens have expired
                        retry = ts + window_ms - now
                        break
                return None, used, max(retry, 1)
            cur = c.execute("INSERT INTO usage(key_hash, ts_ms, tokens) VALUES (?,?,?)",
                            (key_hash, now, tokens))
            return cur.lastrowid, used + tokens, 0
        return await self._run(self._tx, work)

    async def settle(self, row_id: int, key_hash: str, tokens: int, window_ms: int, now: int) -> int:
        """Set a reservation to its real size; return the key's window usage afterwards."""
        def work(c: sqlite3.Connection) -> int:
            changed = c.execute("UPDATE usage SET tokens=? WHERE id=?", (tokens, row_id)).rowcount
            if not changed:  # the reservation aged out of the table (a very slow request)
                c.execute("INSERT INTO usage(key_hash, ts_ms, tokens) VALUES (?,?,?)", (key_hash, now, tokens))
            return c.execute("SELECT COALESCE(SUM(tokens),0) FROM usage WHERE key_hash=? AND ts_ms>?",
                             (key_hash, now - window_ms)).fetchone()[0]
        return await self._run(self._tx, work)

    async def release(self, row_id: int) -> None:
        await self._run(self._tx, lambda c: c.execute("DELETE FROM usage WHERE id=?", (row_id,)))

    async def purge(self, window_ms: int, now: int) -> int:
        """Delete every expired row (all keys). Returns how many were removed."""
        return await self._run(self._tx, lambda c: c.execute(
            "DELETE FROM usage WHERE ts_ms<=?", (now - window_ms,)).rowcount)

    async def row_count(self) -> int:
        return await self._run(lambda: self._conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0])

    async def usage(self, key_hash: str, window_ms: int, now: int) -> int:
        return await self._run(lambda: self._conn.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM usage WHERE key_hash=? AND ts_ms>?",
            (key_hash, now - window_ms)).fetchone()[0])


class TokenRateLimiter:
    def __init__(self, store: UsageStore, limit: int = DEFAULT_LIMIT, window_ms: int = DEFAULT_WINDOW_MS,
                 clock: Callable[[], int] = wall_clock_ms) -> None:
        self.store, self.limit, self.window_ms, self.clock = store, limit, window_ms, clock

    async def reserve(self, api_key: str, tokens: int) -> Reservation:
        """Admit the request or raise a ``GatewayError`` (413 if it can never fit, else 429)."""
        if tokens > self.limit:
            raise request_too_large()
        key_hash = hash_key(api_key)
        row_id, used, retry_ms = await self.store.reserve(key_hash, tokens, self.limit, self.window_ms,
                                                          self.clock())
        if row_id is None:
            raise rate_limit_exceeded(math.ceil(retry_ms / 1000))
        return Reservation(row_id, key_hash, tokens, used)

    async def settle(self, reservation: Reservation, actual_tokens: int) -> int:
        """Record real usage; returns the tokens still available in the window."""
        used = await self.store.settle(reservation.id, reservation.key_hash, max(actual_tokens, 0),
                                       self.window_ms, self.clock())
        return max(self.limit - used, 0)

    async def release(self, reservation: Reservation) -> None:
        await self.store.release(reservation.id)

    async def purge(self) -> int:
        return await self.store.purge(self.window_ms, self.clock())
