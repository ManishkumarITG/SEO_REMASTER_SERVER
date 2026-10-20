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
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum

from services.crawler.robots import RobotsRules

#: ~16 req/s ceiling. At the measured ~6 items/s this never binds normally; it is
#: a safety rail, not a throttle. Also the absolute floor: no caller may go faster.
PER_HOST_MIN_INTERVAL_S = 0.06
#: A hostile `Crawl-delay: 600` cannot stall a job for hours.
CRAWL_DELAY_CLAMP_S = 2.0
#: Default ceiling after back-off. Low enough that a head-only catalog pass keeps
#: moving; a caller that would rather wait than give up raises it per gate.
MAX_HOST_INTERVAL_S = 2.0
#: How fast the interval decays back toward the floor once a host stops
#: complaining. Backing off has to be reversible, or one 429 early in a long
#: crawl slows every remaining page for no reason.
RECOVERY_FACTOR = 0.8


class HostGate:
    """One per crawl (a crawl is one host). Serialises request STARTS so the rate
    ceiling holds regardless of how many workers are in flight."""

    def __init__(
        self,
        interval_s: float = PER_HOST_MIN_INTERVAL_S,
        *,
        max_interval_s: float = MAX_HOST_INTERVAL_S,
    ) -> None:
        self.floor_s = max(interval_s, PER_HOST_MIN_INTERVAL_S)
        self.interval_s = self.floor_s
        self.max_interval_s = max(max_interval_s, self.floor_s)
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    @classmethod
    def for_robots(
        cls,
        robots: RobotsRules,
        *,
        floor_s: float = PER_HOST_MIN_INTERVAL_S,
        max_interval_s: float = MAX_HOST_INTERVAL_S,
    ) -> HostGate:
        """Honour Crawl-delay (clamped), never going below `floor_s`.

        `floor_s` is how a caller asks to be gentler than the global floor: a
        site-wide crawl fetches whole pages rather than <head>s, so it sets a
        higher floor than enrichment's. Omitting it reproduces the original
        behaviour exactly.
        """
        delay = robots.crawl_delay or 0.0
        return cls(max(min(delay, CRAWL_DELAY_CLAMP_S), floor_s), max_interval_s=max_interval_s)

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            due = self._last_start + self.interval_s
            if due > now:
                await asyncio.sleep(due - now)
                now = time.monotonic()
            self._last_start = now

    def back_off(self, retry_after: str | None = None) -> None:
        """Quadruple the interval, honouring Retry-After, up to this gate ceiling."""
        proposed = self.interval_s * 4
        try:
            if retry_after:
                proposed = max(proposed, float(retry_after))
        except ValueError:
            pass
        self.interval_s = min(max(proposed, self.floor_s), self.max_interval_s)

    def recover(self) -> None:
        """Ease back toward the floor after a request the host was happy with.

        Without this, a single 429 in the first minute of a long crawl keeps every
        later request slow, and the crawl runs out of time having learned nothing.
        """
        if self.interval_s > self.floor_s:
            self.interval_s = max(self.floor_s, self.interval_s * RECOVERY_FACTOR)


# --- adaptive rate limiting for the site crawl ------------------------------------
#
# `HostGate` above answers a 429 by multiplying its interval by four PER RESPONSE.
# With six requests in flight one rate-limit episode arrives as five or six 429s,
# so the interval compounds 0.1 -> 0.4 -> 1.6 -> 6.4 -> 25.6 -> 30 s from a single
# event, then decays at x0.8 per page and needs ~150 s to recover. Measured on
# go2marine, that recovery was ~1,500 s of a 1,821 s crawl: the crawler spent 85%
# of its life waiting out its own over-reaction.
#
# The limiter below controls a RATE instead, with two changes that matter:
#
# - Episode coalescing. Every request carries the epoch it was sent under. Only a
#   429 from the CURRENT epoch lowers the rate; the others from the same burst are
#   counted and ignored, so six simultaneous 429s are one signal, not six.
# - AIMD with a learned knee. A throttle cuts the rate multiplicatively (x0.6);
#   success raises it additively, quickly while well below the rate that last drew
#   a 429 (the "knee") and slowly near it. After an episode it cruises just under
#   the knee for a while, doubling that time while the knee holds steady, so a
#   store that tolerates 3 req/s is crawled at ~2.7 req/s with a 429 every several
#   minutes instead of being hammered into a stall every 70 pages.
#
# `AimdState` is pure (it is given `now`, never reads a clock) so the algorithm can
# be tested deterministically and replayed by a Redis-backed limiter later.


class Signal(StrEnum):
    """What a response told us about the host's tolerance."""

    OK = "ok"  # anything the host answered normally, 404 included
    THROTTLE = "throttle"  # 429 / 503
    SEVERE = "severe"  # a bot-challenge page: back off hard
    NEUTRAL = "neutral"  # transport failure: says nothing about our rate


@dataclass(frozen=True, slots=True)
class AimdParams:
    #: Ceiling, from the configured floor interval and robots Crawl-delay.
    r_max: float = 1 / PER_HOST_MIN_INTERVAL_S
    #: Floor. Below one request per five seconds a crawl is no longer useful.
    r_min: float = 0.2
    #: Starting rate. None starts at `r_max`, which is how the old gate behaved and
    #: what keeps directly-constructed limiters (tests) as fast as before.
    initial_rate: float | None = None
    #: Grow x1.25 per round until the first throttle, from a cautious start.
    slow_start: bool = False
    #: A previous crawl's knee for this host, so a re-crawl starts informed.
    known_knee: float | None = None
    beta: float = 0.6
    beta_severe: float = 0.36
    #: Additive increase per round while below `knee_margin` x knee (req/s).
    increase_below_knee: float = 0.5
    #: Increase per round near the knee, as a fraction of the knee.
    increase_near_knee: float = 0.02
    knee_margin: float = 0.85
    #: While cruising, the rate is held at this fraction of the knee. It is a
    #: starting point, not a constant: see `AimdState.cruise_fraction`.
    cruise_fraction: float = 0.9
    #: The fraction is learned down to this, and no further.
    cruise_fraction_floor: float = 0.5
    cruise_min_s: float = 60.0
    cruise_max_s: float = 600.0
    #: The knee counts as stable (so the next cruise doubles) when an episode
    #: moves it by less than this fraction. 25%, not 10%: probing noise alone
    #: moves the knee ~10-20% per episode, so a tight band kept resetting the
    #: cruise to 60s and probed every minute or two - and on a store that locks a
    #: client out after a 429, every probe costs a 15-20s lockout. In simulation
    #: against lockout stores the wider band halved the episodes and took 429s
    #: from 1.3-2.7% to 0.7-1.0% of requests, with throughput up, not down.
    knee_stable_band: float = 0.25
    #: A throttle pauses the host for this many send slots at the new rate.
    pause_slots: float = 4.0
    pause_min_s: float = 0.25
    pause_max_s: float = 30.0
    severe_pause_s: float = 60.0
    retry_after_max_s: float = 60.0
    #: Episodes this close together escalate the pause (x2, then x4).
    escalation_window_s: float = 60.0


#: Fraction of the knee a learned rate is reported at. Shared by `AimdState` and
#: `LocalRateLimiter.for_robots`, which inverts it to recover a previous knee.
CRUISE_FRACTION = AimdParams().cruise_fraction  # an instance: on a slots class it is a descriptor


@dataclass(frozen=True, slots=True)
class Ticket:
    """Permission to send one request: when, and under which epoch."""

    start: float
    epoch: int


class AimdState:
    """The limiter's arithmetic, with no I/O and no clock of its own."""

    __slots__ = (
        "params", "rate", "knee", "slow_start", "epoch", "next_at", "pause_until",
        "round_ok", "cruise_until", "cruise_s", "episodes", "throttled", "ok_count",
        "pause_total_s", "cruise_fraction", "lockout_s", "_cruise_credited",
        "_episode_times", "_recovered", "_chain_start", "_last_refused_send", "_last_pause",
    )

    def __init__(self, params: AimdParams, *, now: float = 0.0) -> None:
        self.params = params
        start = params.initial_rate if params.initial_rate else params.r_max
        self.rate = min(params.r_max, max(params.r_min, start))
        self.knee = params.known_knee
        self.slow_start = params.slow_start and params.known_knee is None
        self.epoch = 0
        self.next_at = now
        self.pause_until = 0.0
        self.round_ok = 0
        self.cruise_until = 0.0
        self.cruise_s = params.cruise_min_s
        self.episodes = 0
        self.throttled = 0
        self.ok_count = 0
        #: Seconds of host-wide pause imposed by throttles, for the crawl report.
        self.pause_total_s = 0.0
        #: The share of the knee we cruise at, LEARNED. A host with a burst bucket
        #: lets us exceed its sustainable rate until the bucket runs dry, so the
        #: rate at which the 429 finally arrives - the knee - overstates what it
        #: can take, and 0.9 x knee can still be too fast. An episode DURING a
        #: cruise is proof of that, and lowers this; a cruise that ends cleanly
        #: earns a little back, so a host that speeds up is noticed.
        self.cruise_fraction = params.cruise_fraction
        self._cruise_credited = True
        self._episode_times: deque[float] = deque()
        #: How long this host keeps refusing after it first says 429, LEARNED.
        #: Shopify storefronts lock a client out for a window (~10-20s measured on
        #: go2marine) and refuse everything in it whatever our rate, so each retry
        #: sent inside the window is a wasted 429. Once known, an episode simply
        #: waits the window out in one pause.
        self.lockout_s: float | None = None
        #: Whether the host has answered normally since the last episode. A 429
        #: with no success in between is the SAME lockout, not new evidence that
        #: our rate is too high, so it lengthens the pause instead of cutting again.
        self._recovered = True
        #: SEND times (ticket starts), not arrival times: when the first refused
        #: request of the current lockout went out, and the latest one. Measuring
        #: from arrivals would fold our own back-off overshoot into the estimate
        #: and roughly double it.
        self._chain_start: float | None = None
        self._last_refused_send = 0.0
        self._last_pause = 0.0

    def reserve(self, now: float) -> Ticket:
        """Book the next send slot. Slots are 1/rate apart and never inside a pause."""
        start = max(now, self.next_at, self.pause_until)
        self.next_at = start + 1.0 / self.rate
        return Ticket(start=start, epoch=self.epoch)

    def still_valid(self, ticket: Ticket) -> bool:
        """False for a slot booked before the latest throttle: it was spaced for a
        rate the host has since refused, so it must be re-booked, not fired."""
        return ticket.epoch == self.epoch

    def on_ok(self, ticket: Ticket, now: float) -> None:
        self.ok_count += 1
        if ticket.epoch != self.epoch:
            return  # a success from before the last cut says nothing about the new rate
        if not self._recovered:
            # First normal answer since the host started refusing: the lockout is
            # over, and how long it lasted is worth remembering. It ended somewhere
            # between the last refused send and this successful one; the midpoint
            # is the estimate.
            self._recovered = True
            if self._chain_start is not None:
                ended = (self._last_refused_send + ticket.start) / 2
                observed = max(0.0, ended - self._chain_start)
                self.lockout_s = observed if self.lockout_s is None else 0.5 * self.lockout_s + 0.5 * observed
                self._chain_start = None
        self.round_ok += 1
        # One "round" is about a second of traffic at the current rate, so growth
        # is per unit of time, not per request.
        if self.round_ok < max(1, math.ceil(self.rate)):
            return
        self.round_ok = 0
        p = self.params
        if self.slow_start:
            self.rate = min(p.r_max, self.rate * 1.25)
            return
        if self.knee is None:
            step, cap = p.increase_below_knee, p.r_max
        else:
            if now >= self.cruise_until and not self._cruise_credited:
                # A whole cruise passed without a single throttle: the fraction
                # was safe, so try a little closer to the knee next time.
                self._cruise_credited = True
                self.cruise_fraction = min(p.cruise_fraction, self.cruise_fraction + 0.02)
            near = self.rate >= p.knee_margin * self.knee
            step = p.increase_near_knee * self.knee if near else p.increase_below_knee
            cap = self.cruise_fraction * self.knee if now < self.cruise_until else p.r_max
        if self.rate < cap:
            self.rate = min(cap, self.rate + step)
        if self.knee is not None and self.rate > self.knee:
            # A full round above the knee without a single refusal: the knee is a
            # stale, pessimistic reading (from a lockout, or a quieter store), so
            # it rises with us. Otherwise growth stays pinned at 2% of a knee the
            # store has already shown it does not care about.
            self.knee = self.rate

    def on_throttle(
        self, ticket: Ticket, now: float, *, retry_after_s: float | None = None, severe: bool = False
    ) -> bool:
        """Record a throttle. -> True when it started a NEW episode (the rate was cut)."""
        p = self.params
        self.throttled += 1
        if retry_after_s is not None and retry_after_s > 0:
            # Host-wide, never stacked: the furthest pause asked for wins.
            self._pause_to(now + min(retry_after_s, p.retry_after_max_s), now)
        if ticket.epoch != self.epoch:
            return False  # same burst as a 429 already acted on: counted, not acted on

        # New epoch either way, so slots booked before this answer are re-booked.
        self.epoch += 1
        self.round_ok = 0
        if not self._recovered:
            # Refused again with no success since the last cut: still locked out.
            # The rate is not what the host is objecting to - time is - so cutting
            # it again would only collapse the crawl (measured: 1.46 -> 0.88 -> 0.53
            # req/s on one lockout). Wait longer instead, doubling each time.
            self._last_refused_send = ticket.start
            self._last_pause = min(p.retry_after_max_s, max(p.pause_min_s, self._last_pause * 2))
            self._pause_to(now + self._last_pause, now)
            return False

        self.episodes += 1
        self.slow_start = False
        self._recovered = False
        self._chain_start = ticket.start
        self._last_refused_send = ticket.start
        if now < self.cruise_until:
            # Throttled while holding to the cruise cap: the cap itself is too fast.
            self.cruise_fraction = max(p.cruise_fraction_floor, self.cruise_fraction * 0.9)
        self._cruise_credited = False
        previous_knee = self.knee
        if previous_knee is None:
            self.knee = self.rate
        elif self.rate < previous_knee:
            # Refused BELOW the knee: the knee is stale and optimistic, so close
            # half the gap at once instead of creeping down 30% per episode.
            self.knee = 0.5 * previous_knee + 0.5 * self.rate
        else:
            self.knee = 0.7 * previous_knee + 0.3 * self.rate
        # Cut, and never land above the cruise cap. A probe that overshot to 8
        # req/s and drained the host's burst would otherwise be cut to 4.8, still
        # too fast with an empty bucket, and draw a second 429 at once - which
        # would then be blamed on the cruise cap and wrongly shrink it.
        cut = self.rate * (p.beta_severe if severe else p.beta)
        self.rate = max(p.r_min, min(cut, self.cruise_fraction * self.knee))

        self._episode_times.append(now)
        while self._episode_times and now - self._episode_times[0] > p.escalation_window_s:
            self._episode_times.popleft()
        recent = len(self._episode_times)
        escalation = 1 if recent <= 2 else min(4, 2 ** (recent - 2))
        pause = min(p.pause_max_s, max(p.pause_min_s, p.pause_slots / self.rate * escalation))
        if self.lockout_s is not None:
            # This host is known to refuse for about this long after a 429, counted
            # from when the refused request was SENT: wait it out once (with a 10%
            # margin), rather than spending retries discovering it again.
            lockout_ends = ticket.start + 1.1 * min(p.retry_after_max_s, self.lockout_s)
            pause = max(pause, lockout_ends - now)
        if severe:
            pause = max(pause, p.severe_pause_s)
        self._last_pause = pause
        self._pause_to(now + pause, now)

        moved = previous_knee is None or abs(self.knee - previous_knee) > p.knee_stable_band * previous_knee
        self.cruise_s = p.cruise_min_s if moved else min(p.cruise_max_s, self.cruise_s * 2)
        self.cruise_until = now + self.cruise_s
        return True

    def _pause_to(self, until: float, now: float) -> None:
        if until > self.pause_until:
            self.pause_total_s += until - max(self.pause_until, now)
            self.pause_until = until
        self.next_at = max(self.next_at, self.pause_until)

    @property
    def learned_rps(self) -> float | None:
        """The sustainable rate this host showed us, for the next crawl to start at.

        From the knee when the host ever pushed back, at the cruise fraction this
        crawl LEARNED rather than the default, so the next crawl starts at a rate
        the host actually sustained. Without a single throttle it is a lower bound
        (the host took everything we sent), reported only once enough requests
        succeeded for that to mean something.
        """
        if self.knee is not None:
            return round(self.cruise_fraction * self.knee, 3)
        if self.ok_count >= 100:
            return round(self.rate, 3)
        return None


def parse_retry_after(value: str | None, server_date: str | None = None) -> float | None:
    """Seconds from a Retry-After header, numeric OR HTTP-date. None when unusable.

    A date is measured against the response's own Date header when present, so the
    local clock (which drifts under WSL2 and after sleep) never enters into it.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
        reference = parsedate_to_datetime(server_date) if server_date else datetime.now(UTC)
    except (TypeError, ValueError, IndexError):
        return None
    if target is None or reference is None or target.tzinfo is None or reference.tzinfo is None:
        return None
    return max(0.0, (target - reference).total_seconds())


class LocalRateLimiter:
    """`AimdState` for one process. Asyncio is single-threaded, so each state
    transition is atomic by construction and no lock is ever held while sleeping:
    book a slot, sleep until it, then check the booking is still valid."""

    def __init__(
        self,
        params: AimdParams,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        poll_s: float = 0.5,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s
        self.state = AimdState(params, now=clock())

    @classmethod
    def for_robots(
        cls,
        robots: RobotsRules,
        *,
        floor_s: float = PER_HOST_MIN_INTERVAL_S,
        initial_rate: float | None = None,
        slow_start: bool = False,
        known_rate: float | None = None,
        pause_max_s: float = 30.0,
        retry_after_max_s: float = 60.0,
    ) -> LocalRateLimiter:
        """The same ceiling `HostGate.for_robots` computes, expressed as a rate.

        `known_rate` is a previous crawl's learned rate for this host: the crawl
        starts there and treats it as the knee instead of rediscovering it.
        """
        delay = robots.crawl_delay or 0.0
        interval = max(min(delay, CRAWL_DELAY_CLAMP_S), floor_s, PER_HOST_MIN_INTERVAL_S)
        r_max = 1.0 / interval
        known_knee = None
        if known_rate and known_rate > 0:
            initial_rate = min(known_rate, r_max)
            known_knee = known_rate / CRUISE_FRACTION
            slow_start = False
        return cls(AimdParams(
            r_max=r_max,
            initial_rate=initial_rate,
            slow_start=slow_start,
            known_knee=known_knee,
            pause_max_s=pause_max_s,
            retry_after_max_s=retry_after_max_s,
        ))

    async def acquire(self, should_stop: Callable[[], bool] | None = None) -> Ticket | None:
        """Wait for a send slot. None when `should_stop` turned true while waiting.

        The wait is sliced so Stop and the deadline interrupt a long pause within
        `poll_s`, instead of a person pressing Stop and watching nothing happen for
        the length of a back-off.
        """
        while True:
            ticket = self.state.reserve(self._clock())
            while True:
                if should_stop is not None and should_stop():
                    return None
                delay = ticket.start - self._clock()
                if delay <= 0:
                    break
                await self._sleep(min(delay, self._poll_s))
            if self.state.still_valid(ticket):
                return ticket
            # A throttle landed while we slept: re-book at the new rate.

    async def wait(self) -> None:
        """`HostGate.wait`-compatible: take a slot and send without reporting."""
        await self.acquire()

    def report(self, ticket: Ticket, signal: Signal, *, retry_after_s: float | None = None) -> bool:
        """Feed a response back. -> True when it started a new throttle episode."""
        now = self._clock()
        if signal is Signal.OK:
            self.state.on_ok(ticket, now)
        elif signal is Signal.THROTTLE:
            return self.state.on_throttle(ticket, now, retry_after_s=retry_after_s)
        elif signal is Signal.SEVERE:
            return self.state.on_throttle(ticket, now, retry_after_s=retry_after_s, severe=True)
        return False
