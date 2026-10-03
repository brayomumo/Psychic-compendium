"""Capped exponential backoff with full jitter.

After a broker restart, every client notices at the same moment. If they all
retry on the same schedule, they stampede the broker in synchronised waves.
Full jitter (a uniform delay between zero and the exponential ceiling) spreads
them out. Of the strategies compared in the AWS Architecture Blog post
"Exponential Backoff And Jitter", it does the least total work.
"""

import random

# 2**64 times any sane base is far above any cap, and stopping the exponent
# here keeps the arithmetic finite however many attempts have failed.
_MAX_EXPONENT = 64


def ceiling(attempt: int, *, base: float, cap: float) -> float:
    """Returns the upper bound of the delay before retry ``attempt``.

    Args:
        attempt: zero-based count of consecutive failures so far.
        base: ceiling for the first retry, in seconds.
        cap: largest ceiling, in seconds.

    Returns:
        ``min(cap, base * 2**attempt)``, computed without float overflow.

    Raises:
        ValueError: on a negative attempt or invalid bounds.
    """
    if attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt}")
    if not 0 < base <= cap:
        raise ValueError(f"need 0 < base <= cap, got base={base} cap={cap}")
    if attempt >= _MAX_EXPONENT:
        return cap
    return min(cap, base * (1 << attempt))


class Backoff:
    """Hands out jittered delays for consecutive failures of one operation.

    Callers ``reset()`` only after the operation has done useful work, not
    merely after a connection opens. Otherwise a broker that accepts a
    connection and then drops it straight away would be retried in a hot loop.
    """

    def __init__(
        self, base: float, cap: float, rng: random.Random | None = None
    ) -> None:
        """Creates a backoff schedule.

        Args:
            base: ceiling for the first retry, in seconds.
            cap: largest ceiling, in seconds.
            rng: randomness source; pass a seeded one for repeatable tests.
        """
        ceiling(0, base=base, cap=cap)  # Validates the bounds early.
        self._base = base
        self._cap = cap
        self._rng = rng or random.Random()
        self._attempt = 0

    @property
    def attempt(self) -> int:
        """Consecutive failures since the last reset."""
        return self._attempt

    def next_delay(self) -> float:
        """Returns the delay before the next retry and counts the failure."""
        bound = ceiling(self._attempt, base=self._base, cap=self._cap)
        self._attempt += 1
        return self._rng.uniform(0.0, bound)

    def reset(self) -> None:
        """Starts the schedule over after a success."""
        self._attempt = 0
