"""
Upstream OpenCode Go key pool: rotation, cooldown, dead-key tracking.

State is in-process memory, not the database — it needs to be checked on
every request and reset is fine on restart. This means the gateway MUST run
as a single process (uvicorn --workers 1, no external process manager
fanning out multiple copies); two processes would each think a key is
healthy that the other just got a 429 from. deploy/gateway.ps1 runs exactly
one process for this reason — see the note there before changing it.
"""
import logging
import threading
import time

from gateway.config import COOLDOWN_SECONDS, UPSTREAM_KEYS

log = logging.getLogger("gateway.keys")


class UpstreamKeyPool:
    def __init__(self, keys: list[str]):
        if not keys:
            raise RuntimeError(
                "No upstream OpenCode keys configured. Set OPENCODE_API_KEY "
                "(and optionally OPENCODE_KEY_1, OPENCODE_KEY_2, ...) in .env."
            )
        self._keys = list(keys)
        self._lock = threading.Lock()
        self._cursor = 0
        self._cooldown_until: dict[str, float] = {}   # key -> epoch seconds
        self._dead: dict[str, str] = {}                # key -> reason

    def _available(self, now: float) -> list[str]:
        out = []
        for k in self._keys:
            if k in self._dead:
                continue
            cd = self._cooldown_until.get(k, 0)
            if cd and cd > now:
                continue
            out.append(k)
        return out

    def attempt_order(self) -> list[str]:
        """Ordered list of keys worth trying right now, round-robin starting
        point so load spreads across the pool rather than hammering key 0."""
        now = time.time()
        with self._lock:
            avail = self._available(now)
            if not avail:
                # everything is cooling down or dead — try the least-recently
                # cooled-down key anyway rather than hard-failing every call
                candidates = [k for k in self._keys if k not in self._dead]
                if not candidates:
                    return []
                candidates.sort(key=lambda k: self._cooldown_until.get(k, 0))
                return candidates[:1]
            start = self._cursor % len(avail)
            self._cursor += 1
            return avail[start:] + avail[:start]

    def mark_429(self, key: str, retry_after: float | None = None):
        with self._lock:
            self._cooldown_until[key] = time.time() + (retry_after or COOLDOWN_SECONDS)
        log.warning(f"key {key[:10]}... cooling down for "
                    f"{retry_after or COOLDOWN_SECONDS:.0f}s (429)")

    def mark_dead(self, key: str, reason: str):
        with self._lock:
            self._dead[key] = reason
        log.error(f"key {key[:10]}... marked DEAD: {reason}")

    def mark_ok(self, key: str):
        """A success clears any cooldown — the account recovered before the
        timer did."""
        with self._lock:
            self._cooldown_until.pop(key, None)

    def status(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = []
            for k in self._keys:
                cd = self._cooldown_until.get(k, 0)
                out.append({
                    "prefix": k[:10] + "...",
                    "dead": k in self._dead,
                    "dead_reason": self._dead.get(k),
                    "cooling_down": bool(cd and cd > now),
                    "cooldown_seconds_left": max(0, round(cd - now)) if cd else 0,
                })
            return out


pool = UpstreamKeyPool(UPSTREAM_KEYS)
