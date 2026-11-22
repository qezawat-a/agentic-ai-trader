"""Execution limits for J+Rock's agentic work.

Two different guards, for two different failure modes:

``RateLimiter``
    A ROLLING-WINDOW throttle on outbound provider calls. It protects the
    API key and the account from a runaway loop (a self-heal cycle that
    retries, a burst of commands) and from a provider that answers 429
    because it is already unhappy. It never raises: the caller gets a
    refusal it can show the operator, with the number of seconds until the
    window frees a slot.

``StepBudget``
    A per-operation budget: how many steps ONE operation may take. A step
    is one unit of work the agent does on its own initiative - a model
    call, an executed tool command, a file edit. The budget stops a loop
    that would otherwise run forever (or run very expensively) and it
    fails LOUDLY: the operation reports that it stopped because the budget
    ran out, rather than pretending it finished.

Neither limit can be turned off by accident: ``max_steps <= 0`` means
"unlimited" (documented), while ``max_calls <= 0`` means "no provider
calls allowed" - the opposite, because a throttle of zero that silently
allowed everything would be a lie.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """Rolling-window limiter for outbound agent/provider calls.

    ``window_sec`` is the width of the window; ``max_calls`` is how many
    calls may start inside it. A call that arrives when the window is full
    is refused together with the number of seconds until the oldest call
    ages out of the window.
    """

    window_sec: float = 128.0
    max_calls: int = 128
    _calls: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def allow(self) -> tuple[bool, float]:
        """Try to consume one slot.

        Returns ``(allowed, retry_after_sec)``. ``retry_after_sec`` is 0
        when the call is allowed.
        """
        now = time.time()
        with self._lock:
            cutoff = now - max(0.0, self.window_sec)
            self._calls = [t for t in self._calls if t > cutoff]
            if self.max_calls <= 0:
                return False, max(0.0, self.window_sec)
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True, 0.0
            oldest = min(self._calls) if self._calls else now
            return False, max(0.0, oldest + self.window_sec - now)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()

    def reserve(self, n: int) -> tuple[bool, float]:
        """Reserve ``n`` slots at once.

        Some operations cost more than one provider call: the model
        selector probes up to a dozen models before it picks one. Reserving
        the whole batch keeps the limiter honest - otherwise a single
        "find me a model" action could burst past the cap that a chat
        request respects.
        """
        n = max(1, int(n))
        now = time.time()
        with self._lock:
            cutoff = now - max(0.0, self.window_sec)
            self._calls = [t for t in self._calls if t > cutoff]
            if self.max_calls <= 0:
                return False, max(0.0, self.window_sec)
            if len(self._calls) + n > self.max_calls:
                oldest = min(self._calls) if self._calls else now
                return False, max(0.0, oldest + self.window_sec - now)
            self._calls.extend([now] * n)
            return True, 0.0

    def used(self) -> int:
        """How many calls are currently inside the window."""
        now = time.time()
        cutoff = now - max(0.0, self.window_sec)
        with self._lock:
            return sum(1 for t in self._calls if t > cutoff)


@dataclass
class StepBudget:
    """The step budget of ONE operation.

    ``max_steps <= 0`` means unlimited. ``spent`` counts the steps that
    were taken; ``step()`` consumes one and returns False once the budget
    is gone.
    """

    max_steps: int = 128
    label: str = ""
    spent: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def exhausted(self) -> bool:
        return self.max_steps > 0 and self.spent >= self.max_steps

    @property
    def remaining(self) -> int:
        if self.max_steps <= 0:
            return -1  # unlimited
        return max(0, self.max_steps - self.spent)

    def step(self) -> bool:
        """Consume one step. Returns False when the budget is exhausted."""
        with self._lock:
            if self.max_steps > 0 and self.spent >= self.max_steps:
                return False
            self.spent += 1
            return True

    def reset(self, max_steps: int | None = None, label: str | None = None) -> None:
        with self._lock:
            if max_steps is not None:
                self.max_steps = max_steps
            if label is not None:
                self.label = label
            self.spent = 0

    def note(self) -> str:
        """A one-line, honest description of the budget state."""
        if self.max_steps <= 0:
            return f"{self.label or 'agent'} steps: unlimited (used {self.spent})"
        return (f"{self.label or 'agent'} steps: {self.spent}/{self.max_steps} "
                f"used, {self.remaining} left")
