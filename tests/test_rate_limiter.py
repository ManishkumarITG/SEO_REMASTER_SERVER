"""The adaptive limiter that replaced the site crawl's compounding back-off.

Everything here runs on a fake clock: `AimdState` is given `now` and never reads
one, and `LocalRateLimiter` takes an injectable clock and sleep. Nothing sleeps
for real, so the suite is fast and every number is reproducible.

The simulation at the bottom is the test that matters most. It puts six workers
against a fake host that rate-limits like a token bucket (which is roughly how a
storefront edge behaves) and checks the two things the old gate got wrong: that
we keep up with what the host allows, and that we stop hammering it.
"""

from __future__ import annotations

import heapq
import itertools

import pytest

from services.crawler.robots import RobotsRules
from services.crawler.throttle import (
    AimdParams,
    AimdState,
    LocalRateLimiter,
    Signal,
    parse_retry_after,
)

PARAMS = AimdParams(r_max=10.0)


def fresh(params: AimdParams = PARAMS) -> AimdState:
    return AimdState(params, now=0.0)


def recover(state: AimdState, now: float) -> None:
    """One normal answer, so the next 429 counts as a NEW episode rather than
    the same lockout continuing."""
    state.on_ok(state.reserve(now), now)


# --- episode coalescing ---------------------------------------------------------------


def test_six_simultaneous_429s_are_one_episode_not_six() -> None:
    """The bug that cost go2marine ~1,500 s: each 429 compounded the back-off."""
    state = fresh()
    tickets = [state.reserve(0.0) for _ in range(6)]
    acted = [state.on_throttle(t, 0.7) for t in tickets]

    assert acted == [True, False, False, False, False, False]
    assert state.episodes == 1
    assert state.throttled == 6  # every response is still counted
    assert state.rate == pytest.approx(6.0)  # cut once, x0.6
    # The pause is a few send slots at the new rate, not 30 seconds.
    assert state.pause_until - 0.7 == pytest.approx(4 / 6.0)


def test_a_ticket_booked_before_a_throttle_must_be_rebooked() -> None:
    state = fresh()
    early = state.reserve(0.0)
    late = state.reserve(0.0)
    state.on_throttle(early, 0.05)
    assert not state.still_valid(late)  # spaced for a rate the host refused
    rebooked = state.reserve(0.06)
    assert state.still_valid(rebooked)
    assert rebooked.start >= state.pause_until


def test_a_success_from_before_the_cut_does_not_raise_the_rate() -> None:
    state = fresh()
    stale = state.reserve(0.0)
    trigger = state.reserve(0.0)
    state.on_throttle(trigger, 0.1)
    rate = state.rate
    for _ in range(50):
        state.on_ok(stale, 1.0)
    assert state.rate == rate


# --- growth -----------------------------------------------------------------------


def test_growth_is_per_round_of_traffic_and_capped_at_r_max() -> None:
    state = fresh(AimdParams(r_max=5.0, initial_rate=2.0))
    ticket = state.reserve(0.0)
    state.on_ok(ticket, 0.1)
    assert state.rate == 2.0  # one OK is not a round at 2 req/s
    state.on_ok(ticket, 0.2)
    assert state.rate == pytest.approx(2.5)  # +0.5 after ceil(2) OKs
    for i in range(200):
        state.on_ok(ticket, 1.0 + i)
    assert state.rate == 5.0


def test_slow_start_grows_multiplicatively_until_the_first_throttle() -> None:
    state = fresh(AimdParams(r_max=10.0, initial_rate=4.0, slow_start=True))
    ticket = state.reserve(0.0)
    for _ in range(4):  # one round at 4 req/s
        state.on_ok(ticket, 0.5)
    assert state.rate == pytest.approx(5.0)
    state.on_throttle(ticket, 1.0)
    assert not state.slow_start


def test_after_an_episode_the_rate_cruises_just_under_the_knee() -> None:
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0)  # knee 10, rate 6, cruise 60 s
    assert state.knee == pytest.approx(10.0)
    ticket = state.reserve(1.0)
    for i in range(500):
        state.on_ok(ticket, 1.0 + i * 0.1)  # all inside the 60 s cruise window
    assert state.rate == pytest.approx(9.0)  # 0.9 x knee, not r_max
    for i in range(500):
        state.on_ok(ticket, 100.0 + i)  # cruise over: probing resumes
    assert state.rate == pytest.approx(10.0)


def test_a_cut_never_lands_above_the_cruise_cap() -> None:
    """A probe that overshot (here to 9.5 with the knee at 6) is cut to the cap,
    not to 0.6 x 9.5 = 5.7, which would still be too fast for a drained host."""
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0)  # knee 10
    recover(state, 100.0)
    state.knee, state.rate = 6.0, 9.5
    state.on_throttle(state.reserve(200.0), 200.0)
    assert state.rate <= state.cruise_fraction * state.knee + 1e-9


def test_a_throttle_during_cruise_lowers_the_cruise_fraction_and_a_clean_cruise_restores_it() -> None:
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0)  # cruise until 60 s
    recover(state, 5.0)
    state.on_throttle(state.reserve(10.0), 10.0)  # inside it: the cap was too fast
    assert state.cruise_fraction == pytest.approx(0.81)
    ticket = state.reserve(500.0)
    for _ in range(20):
        state.on_ok(ticket, 500.0)  # long after the cruise ended
    assert state.cruise_fraction == pytest.approx(0.83)


def test_the_rate_never_falls_below_r_min() -> None:
    state = fresh(AimdParams(r_max=10.0, r_min=0.5))
    for i in range(30):
        state.on_throttle(state.reserve(i * 100.0), i * 100.0)
        recover(state, i * 100.0 + 50.0)
    assert state.rate == 0.5


# --- lockouts ---------------------------------------------------------------------
#
# Measured on go2marine: after a 429 the store refused everything for ~20 s, at
# any rate. The limiter used to read each refused retry as a new episode and cut
# again, collapsing 1.46 -> 0.88 -> 0.53 req/s on a single lockout.


def test_a_lockout_is_waited_out_not_cut_into() -> None:
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0)
    rate_after_first_cut = state.rate
    pauses = [state.pause_until]
    for _ in range(3):  # refused again, with no success in between
        retry = state.reserve(state.pause_until)
        state.on_throttle(retry, retry.start + 0.5)
        pauses.append(state.pause_until - (retry.start + 0.5))
    assert state.episodes == 1  # one lockout, not four episodes
    assert state.rate == rate_after_first_cut  # the rate was never the problem
    assert pauses[2] == pytest.approx(pauses[1] * 2)  # waiting longer instead
    assert pauses[3] == pytest.approx(pauses[2] * 2)


def test_the_lockout_length_is_learned_from_send_times_and_waited_out_next_time() -> None:
    state = fresh()
    first = state.reserve(10.0)  # refused, sent at 10
    state.on_throttle(first, 10.5)
    for sent in (13.0, 19.0):  # still refused
        state.on_throttle(state.reserve(sent), sent + 0.5)
    ok = state.reserve(31.0)  # first success, sent at 31
    state.on_ok(ok, 31.5)
    # It ended between the last refusal (19) and the success (31): ~25 - 10 = 15 s.
    assert state.lockout_s == pytest.approx(15.0)
    refused = state.reserve(200.0)
    state.on_throttle(refused, 200.5)
    # The next episode waits the whole lockout out in one pause, with a margin.
    assert state.pause_until >= refused.start + 15.0


def test_a_success_from_before_the_lockout_does_not_end_it() -> None:
    state = fresh()
    stale = state.reserve(0.0)
    state.on_throttle(state.reserve(0.0), 0.5)
    state.on_ok(stale, 0.6)  # sent before the 429, so it proves nothing
    state.on_throttle(state.reserve(state.pause_until), state.pause_until + 0.5)
    assert state.episodes == 1


# --- pauses -----------------------------------------------------------------------


def test_repeated_episodes_inside_a_minute_escalate_the_pause() -> None:
    """Real episodes - the host recovered in between - escalate x2 then x4."""
    state = fresh()
    pauses = []
    now = 0.0
    for _ in range(4):
        state.on_throttle(state.reserve(now), now)
        pauses.append(state.pause_until - now)
        now = state.pause_until + 0.01
        recover(state, now)
    assert state.episodes == 4
    # x1, x1, then x2 and x4 on the 3rd and 4th episode within 60 s.
    assert pauses[2] > pauses[1] * 1.5
    assert pauses[3] > pauses[2] * 1.5


def test_a_bot_challenge_backs_off_hard() -> None:
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0, severe=True)
    assert state.rate == pytest.approx(3.6)  # x0.36, not x0.6
    assert state.pause_until >= 60.0


def test_retry_after_pauses_the_whole_host_and_never_stacks() -> None:
    state = fresh()
    first, second = state.reserve(0.0), state.reserve(0.0)
    state.on_throttle(first, 1.0, retry_after_s=20.0)
    state.on_throttle(second, 1.5, retry_after_s=5.0)  # a shorter ask cannot shorten it
    assert state.pause_until == pytest.approx(21.0)
    assert state.reserve(2.0).start == pytest.approx(21.0)


def test_retry_after_is_capped() -> None:
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0, retry_after_s=3600)
    assert state.pause_until == pytest.approx(PARAMS.retry_after_max_s)


def test_pause_time_is_accounted_without_double_counting_overlaps() -> None:
    state = fresh()
    a, b = state.reserve(0.0), state.reserve(0.0)
    state.on_throttle(a, 0.0, retry_after_s=10.0)
    state.on_throttle(b, 5.0, retry_after_s=10.0)  # overlaps the first pause by 5 s
    assert state.pause_total_s == pytest.approx(15.0)


# --- Retry-After parsing ----------------------------------------------------------


def test_retry_after_numeric_and_http_date_forms() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after("-3") == 0.0
    # A date is measured against the server's own Date header, so local clock
    # skew cannot inflate or erase it.
    assert parse_retry_after(
        "Wed, 21 Oct 2026 07:28:30 GMT", "Wed, 21 Oct 2026 07:28:00 GMT"
    ) == 30.0
    assert parse_retry_after(
        "Wed, 21 Oct 2026 07:28:00 GMT", "Wed, 21 Oct 2026 07:29:00 GMT"
    ) == 0.0


@pytest.mark.parametrize("value", [None, "", "soon", "Wed, 99 Foo 2026"])
def test_unusable_retry_after_is_ignored(value: str | None) -> None:
    assert parse_retry_after(value) is None


# --- learned rate -------------------------------------------------------------------


def test_learned_rate_comes_from_the_knee() -> None:
    state = fresh()
    state.on_throttle(state.reserve(0.0), 0.0)
    assert state.learned_rps == pytest.approx(9.0)


def test_learned_rate_without_pushback_needs_enough_evidence() -> None:
    state = fresh(AimdParams(r_max=3.0))
    ticket = state.reserve(0.0)
    for _ in range(99):
        state.on_ok(ticket, 0.0)
    assert state.learned_rps is None
    state.on_ok(ticket, 0.0)
    assert state.learned_rps == 3.0  # a lower bound: the host took all we sent


def test_a_known_rate_starts_the_next_crawl_informed() -> None:
    limiter = LocalRateLimiter.for_robots(RobotsRules.permissive(), floor_s=0.1, known_rate=3.0)
    state = limiter.state
    assert state.rate == 3.0
    assert not state.slow_start
    assert state.knee == pytest.approx(3.0 / 0.9)


def test_crawl_delay_still_sets_the_ceiling() -> None:
    robots = RobotsRules.permissive()
    robots.crawl_delay = 1.0
    limiter = LocalRateLimiter.for_robots(robots, floor_s=0.1)
    assert limiter.state.params.r_max == pytest.approx(1.0)
    robots.crawl_delay = 600.0  # a hostile Crawl-delay is clamped, as before
    assert LocalRateLimiter.for_robots(robots, floor_s=0.1).state.params.r_max == pytest.approx(0.5)


# --- the async wrapper, on a fake clock ---------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_acquire_spaces_requests_at_the_rate() -> None:
    clock = FakeClock()
    limiter = LocalRateLimiter(AimdParams(r_max=4.0), clock=clock, sleep=clock.sleep)
    starts = []
    for _ in range(4):
        ticket = await limiter.acquire()
        assert ticket is not None
        starts.append(clock.now)
    assert [round(b - a, 3) for a, b in zip(starts, starts[1:], strict=False)] == [0.25, 0.25, 0.25]


async def test_stop_interrupts_a_long_pause_within_one_poll() -> None:
    clock = FakeClock()
    limiter = LocalRateLimiter(AimdParams(r_max=4.0), clock=clock, sleep=clock.sleep, poll_s=0.5)
    first = await limiter.acquire()
    limiter.report(first, Signal.THROTTLE, retry_after_s=60)  # host-wide 60 s pause
    stop_at = clock.now + 1.2
    ticket = await limiter.acquire(should_stop=lambda: clock.now >= stop_at)
    assert ticket is None
    assert clock.now - (stop_at - 1.2) < 2.0  # nowhere near the 60 s pause
    assert max(clock.sleeps) <= 0.5


async def test_a_throttle_during_a_wait_rebooks_the_waiter() -> None:
    clock = FakeClock()
    limiter = LocalRateLimiter(AimdParams(r_max=1.0), clock=clock, sleep=clock.sleep, poll_s=10)
    first = await limiter.acquire()
    booked = limiter.state.reserve(clock.now)  # what a second worker would hold
    limiter.report(first, Signal.THROTTLE)
    assert not limiter.state.still_valid(booked)
    ticket = await limiter.acquire()
    assert ticket is not None and ticket.epoch == 1
    assert clock.now >= limiter.state.pause_until


# --- simulation: six workers against a rate-limited host ----------------------------


def simulate(refill: float, *, burst: int = 60, workers: int = 6, latency: float = 0.6,
             duration: float = 900.0, params: AimdParams | None = None,
             lockout: float = 0.0) -> dict[str, float]:
    """Discrete-event run of the limiter against a token-bucket host.

    Every worker loops: book a slot, wait for it (re-booking if a throttle landed
    meanwhile), send, get the answer `latency` later, report it. The host answers
    200 while it has a token and 429 when it does not, refilling `refill` tokens
    a second up to `burst`. With `lockout`, running out also locks the client out:
    every request for that many seconds gets 429, tokens or not - the behaviour
    measured on a real Shopify store.
    """
    params = params or AimdParams(r_max=10.0, initial_rate=4.0, slow_start=True)
    state = AimdState(params, now=0.0)
    tokens, refilled_at, locked_until = float(burst), 0.0, -1.0
    sent = ok = throttled = 0
    peak_rate = 0.0
    events: list[tuple[float, int, str, int, object]] = []
    seq = itertools.count()

    def push(at: float, kind: str, worker: int, payload: object = None) -> None:
        heapq.heappush(events, (at, next(seq), kind, worker, payload))

    for w in range(workers):
        push(0.0, "ready", w)
    while events:
        now, _, kind, worker, payload = heapq.heappop(events)
        if now > duration:
            break
        if kind == "ready":
            ticket = state.reserve(now)
            push(ticket.start, "fire", worker, ticket)
        elif kind == "fire":
            if not state.still_valid(payload):  # type: ignore[arg-type]
                ticket = state.reserve(now)
                push(ticket.start, "fire", worker, ticket)
                continue
            tokens = min(burst, tokens + (now - refilled_at) * refill)
            refilled_at = now
            sent += 1
            peak_rate = max(peak_rate, state.rate)
            if now >= locked_until and tokens >= 1:
                tokens -= 1
                push(now + latency, "ok", worker, payload)
            else:
                if lockout and now >= locked_until:
                    locked_until = now + lockout
                push(now + latency, "429", worker, payload)
        elif kind == "ok":
            ok += 1
            state.on_ok(payload, now)  # type: ignore[arg-type]
            push(now, "ready", worker)
        else:
            throttled += 1
            state.on_throttle(payload, now)  # type: ignore[arg-type]
            push(now, "ready", worker)
    return {
        "ok_per_s": ok / duration,
        "throttled_share": throttled / max(1, sent),
        "episodes": state.episodes,
        "peak_rate": peak_rate,
        "learned": state.learned_rps or 0.0,
    }


@pytest.mark.parametrize("refill", [1.5, 2.0, 3.0, 4.5])
def test_the_crawl_keeps_up_with_what_the_host_allows_and_stops_hammering_it(refill: float) -> None:
    result = simulate(refill)
    # Throughput: at least 80% of what the host will serve.
    assert result["ok_per_s"] >= 0.8 * refill, result
    # Politeness: rate-limit responses stay rare. The old gate ran at ~6.7%.
    assert result["throttled_share"] <= 0.04, result
    # And the configured ceiling is never exceeded.
    assert result["peak_rate"] <= 10.0 + 1e-9, result


# A store that locks a client out after a 429, sized like go2marine (a sustained
# ~1-2 req/s, a 15-20 s lockout). Two cases: a store crawled for the first time
# (slow start from 4 req/s) and one crawled before (starting at its learned rate).
LOCKOUT_STORES = [(1.0, 15, 20.0), (1.5, 20, 15.0), (1.5, 30, 20.0), (2.0, 30, 10.0)]


@pytest.mark.parametrize(("refill", "burst", "lockout"), LOCKOUT_STORES)
def test_a_store_that_locks_us_out_is_still_crawled_at_its_pace_on_first_contact(
    refill: float, burst: int, lockout: float
) -> None:
    result = simulate(refill, burst=burst, lockout=lockout, latency=0.7, duration=1800.0)
    assert result["ok_per_s"] >= 0.8 * refill, result
    assert result["throttled_share"] <= 0.04, result


@pytest.mark.parametrize(("refill", "burst", "lockout"), LOCKOUT_STORES)
def test_a_store_crawled_before_is_barely_throttled_at_all(
    refill: float, burst: int, lockout: float
) -> None:
    informed = AimdParams(r_max=10.0, initial_rate=1.5, known_knee=1.5 / 0.9)
    result = simulate(refill, burst=burst, lockout=lockout, latency=0.7, duration=1800.0,
                      params=informed)
    assert result["ok_per_s"] >= 0.8 * refill, result
    assert result["throttled_share"] <= 0.015, result


def test_the_old_compounding_gate_is_what_this_replaced() -> None:
    """Pins the arithmetic of the failure: five 429s from ONE burst, x4 each,
    drove the old gate from 0.1 s to its 30 s ceiling."""
    from services.crawler.throttle import HostGate

    gate = HostGate(0.1, max_interval_s=30.0)
    for _ in range(5):
        gate.back_off()
    assert gate.interval_s == 30.0
    state = fresh()
    tickets = [state.reserve(0.0) for _ in range(5)]
    for t in tickets:
        state.on_throttle(t, 0.5)
    assert 1 / state.rate < 1.0  # still sending more than once a second
