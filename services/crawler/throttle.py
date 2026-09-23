"""Per-host request pacing, shared by every bulk crawl in the service layer.

`HostGate` started life inside `services/stores/enrichment.py`, which is still
its heaviest user. It moved here unchanged - same constants, same defaults, same
method behaviour - when the site-wide crawler needed the identical control:
both make hundreds of unattended requests to one third-party host, and the rate
ceiling has to be enforced in ONE place or the two crawls disagree about how
polite we are. `enrichment` re-imports it from here, so
`services.stores.enrichment.HostGate` keeps working.

The gate serialises request STARTS rather than wrapping whole requests. Wrapping
would cap concurrency at one; serialising the start instant caps the RATE while
leaving `concurrency` workers in flight, which is the only arrangement that is
both fast and bounded.
"""

from __future__ import annotations

import asyncio
import time

from services.crawler.robots import RobotsRules

#: ~16 req/s ceiling. At the measured ~6 items/s this never binds normally; it is
#: a safety rail, not a throttle. Also the absolute floor: no caller may go faster.
PER_HOST_MIN_INTERVAL_S = 0.06
#: A hostile `Crawl-delay: 600` cannot stall a job for hours.
CRAWL_DELAY_CLAMP_S = 2.0
#: Ceiling after back-off, so a host answering 429 forever cannot park a worker.
MAX_HOST_INTERVAL_S = 2.0


class HostGate:
    """One per crawl (a crawl is one host). Serialises request STARTS so the rate
    ceiling holds regardless of how many workers are in flight."""

    def __init__(self, interval_s: float = PER_HOST_MIN_INTERVAL_S) -> None:
        self.interval_s = max(interval_s, PER_HOST_MIN_INTERVAL_S)
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    @classmethod
    def for_robots(cls, robots: RobotsRules, *, floor_s: float = PER_HOST_MIN_INTERVAL_S) -> HostGate:
        """Honour Crawl-delay (clamped), never going below `floor_s`.

        `floor_s` is how a caller asks to be gentler than the global floor: a
        site-wide crawl fetches whole pages rather than <head>s, so it sets a
        higher floor than enrichment's. Omitting it reproduces the original
        behaviour exactly.
        """
        delay = robots.crawl_delay or 0.0
        return cls(max(min(delay, CRAWL_DELAY_CLAMP_S), floor_s))

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            due = self._last_start + self.interval_s
            if due > now:
                await asyncio.sleep(due - now)
                now = time.monotonic()
            self._last_start = now

    def back_off(self, retry_after: str | None = None) -> None:
        """Quadruple the interval for the rest of the crawl, honouring Retry-After."""
        proposed = self.interval_s * 4
        try:
            if retry_after:
                proposed = max(proposed, float(retry_after))
        except ValueError:
            pass
        self.interval_s = min(max(proposed, PER_HOST_MIN_INTERVAL_S), MAX_HOST_INTERVAL_S)
