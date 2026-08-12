"""
Per-client requests-per-minute limiter. In-memory sliding window — cheap,
and resets on restart, which is fine for a spend guard (the daily token cap
in gateway/store.py is the one that must survive a restart, and it does
because it's computed from logged requests, not from counter state).
"""
import threading
import time
from collections import defaultdict, deque

_WINDOW = 60.0
_hits: dict[int, deque] = defaultdict(deque)
_lock = threading.Lock()


def check_and_record(client_id: int, rpm_limit: int) -> bool:
    """Returns True and records the hit if under limit; False (not recorded)
    if the client is over its per-minute budget."""
    now = time.time()
    with _lock:
        dq = _hits[client_id]
        while dq and dq[0] < now - _WINDOW:
            dq.popleft()
        if len(dq) >= rpm_limit:
            return False
        dq.append(now)
        return True
