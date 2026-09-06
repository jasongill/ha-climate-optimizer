"""Pure thermal-control helpers for Climate Optimizer.

This module intentionally has no Home Assistant imports so the control math can
be replayed and unit tested without starting Home Assistant.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from statistics import median
from typing import Any


TREND_WINDOW = timedelta(minutes=10)
HISTORY_WINDOW = timedelta(minutes=20)
MIN_TREND_SPAN = timedelta(minutes=3)
PROJECTION_MINUTES = 5.0
LEARNING_ALPHA = 0.15
FAST_LEARNING_ALPHA = 0.4
MIN_VALID_RATE = 0.005
MAX_VALID_RATE = 0.5
SENSOR_MOVE_DELTA = 3.0
SENSOR_MOVE_WINDOW = timedelta(minutes=10)
PREDICTIVE_STOP_MIN_CONFIDENCE = 0.5
PREDICTIVE_STOP_MAX_EARLY_DELTA = 0.5


def _mode_direction(mode: str) -> int:
    """Return the expected temperature direction for an active HVAC mode."""
    return -1 if mode == "cool" else 1


def confidence_aware_stop(
    *,
    cooling: bool,
    current: float,
    projected: float,
    target: float,
    confidence: float,
    slope_per_minute: float | None,
) -> tuple[bool, str]:
    """Return whether a cycle should stop without trusting a weak forecast.

    The measured/filtered room temperature always wins. A projection may stop
    a cycle early only when the learned model is at least moderately confident,
    the live trend is moving in the commanded direction, and the early cutoff
    is bounded to at most half a degree.
    """
    measured_reached = current <= target if cooling else current >= target
    if measured_reached:
        return True, "measured target reached"

    if confidence < PREDICTIVE_STOP_MIN_CONFIDENCE:
        return False, "prediction confidence too low"

    moving_correctly = (
        slope_per_minute is not None
        and (slope_per_minute < 0 if cooling else slope_per_minute > 0)
    )
    if not moving_correctly:
        return False, "room trend is not moving toward target"

    early_delta = min(
        PREDICTIVE_STOP_MAX_EARLY_DELTA,
        PREDICTIVE_STOP_MAX_EARLY_DELTA
        * (confidence - PREDICTIVE_STOP_MIN_CONFIDENCE)
        / (1.0 - PREDICTIVE_STOP_MIN_CONFIDENCE),
    )
    early_threshold = target + early_delta if cooling else target - early_delta
    projection_reached = projected <= target if cooling else projected >= target
    within_early_window = (
        current <= early_threshold if cooling else current >= early_threshold
    )
    if projection_reached and within_early_window:
        return True, f"high-confidence projection within {early_delta:.2f}°F"
    return False, "continuing gently toward measured target"


@dataclass(frozen=True)
class TemperatureEstimate:
    """Filtered room temperature and its recent trajectory."""

    filtered: float
    slope_per_minute: float | None
    projected_5m: float


class TemperatureTracker:
    """Keep a small, timestamped room-temperature history."""

    def __init__(self) -> None:
        self._samples: deque[tuple[datetime, float]] = deque()

    def add(self, timestamp: datetime, temperature: float) -> None:
        """Add a sensor sample, replacing duplicate timestamps."""
        if self._samples and timestamp < self._samples[-1][0]:
            self._samples.clear()
        if self._samples and timestamp == self._samples[-1][0]:
            self._samples[-1] = (timestamp, temperature)
        else:
            self._samples.append((timestamp, temperature))

        cutoff = timestamp - HISTORY_WINDOW
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def estimate(self) -> TemperatureEstimate | None:
        """Return a robust current value and least-squares ten-minute slope."""
        if not self._samples:
            return None

        filtered = float(median(value for _, value in list(self._samples)[-3:]))
        newest = self._samples[-1][0]
        cutoff = newest - TREND_WINDOW
        window = [(ts, value) for ts, value in self._samples if ts >= cutoff]

        slope: float | None = None
        if len(window) >= 3 and window[-1][0] - window[0][0] >= MIN_TREND_SPAN:
            origin = window[0][0]
            xs = [(ts - origin).total_seconds() / 60 for ts, _ in window]
            ys = [value for _, value in window]
            x_mean = sum(xs) / len(xs)
            y_mean = sum(ys) / len(ys)
            denominator = sum((x - x_mean) ** 2 for x in xs)
            if denominator:
                slope = (
                    sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
                    / denominator
                )

        projected = filtered
        if slope is not None:
            projected += slope * PROJECTION_MINUTES
        return TemperatureEstimate(filtered, slope, projected)


@dataclass
class RateEstimate:
    """A bounded exponentially weighted thermal-rate estimate."""

    rate: float
    samples: int = 1

    @property
    def confidence(self) -> float:
        """Rise gradually; five observations are only 50% confidence."""
        return min(1.0, self.samples / 10.0)


class ThermalLearner:
    """Continuously learn safe per-mode/per-fan room response.

    The model is deliberately small: active temperature rate by HVAC mode and
    fan, plus post-stop drift by mode. It can improve stop timing, but cannot
    select a stronger fan or a more aggressive equipment setpoint.
    """

    def __init__(self) -> None:
        self._rates: dict[str, RateEstimate] = {}
        self._post_stop: dict[str, RateEstimate] = {}
        self._last: tuple[datetime, float, str | None, str | None] | None = None
        self._settling: tuple[str, datetime, float] | None = None
        self._fast_samples = 0
        self.sensor_move_count = 0

    @staticmethod
    def _key(
        mode: str, fan_mode: str | None, outdoor_temperature: float | None = None
    ) -> str:
        base = f"{mode}:{fan_mode or 'unknown'}"
        if outdoor_temperature is None:
            return base
        outdoor_bucket = math.floor(outdoor_temperature / 10.0) * 10
        return f"{base}:outdoor_{outdoor_bucket}"

    @staticmethod
    def _update(
        estimates: dict[str, RateEstimate], key: str, value: float, alpha: float
    ) -> None:
        old = estimates.get(key)
        if old is None:
            estimates[key] = RateEstimate(value)
            return
        # Clip each innovation so a sensor glitch cannot rewrite the model.
        bounded = max(old.rate - 0.08, min(old.rate + 0.08, value))
        old.rate = (1 - alpha) * old.rate + alpha * bounded
        old.samples = min(1000, old.samples + 1)

    def reset_observation(self) -> None:
        """Break the sample interval after missing or unconfirmed operation."""
        self._last = None
        self._settling = None

    def observe(
        self,
        timestamp: datetime,
        temperature: float,
        mode: str | None,
        fan_mode: str | None,
        outdoor_temperature: float | None = None,
    ) -> None:
        """Consume one filtered observation and learn from valid intervals."""
        previous = self._last
        if previous is None:
            self._last = (timestamp, temperature, mode, fan_mode)
            return
        prev_time, prev_temp, prev_mode, prev_fan = previous
        if temperature == prev_temp and mode == prev_mode and fan_mode == prev_fan:
            # Do not turn a quantized sensor's eventual 0.1°F step into an
            # artificially fast 30-second rate. Measure from the last actual
            # temperature or actuator change instead.
            return
        elapsed = (timestamp - prev_time).total_seconds() / 60.0
        if elapsed <= 0:
            return
        if elapsed > 30:
            self._last = (timestamp, temperature, mode, fan_mode)
            return

        delta = temperature - prev_temp
        if (
            elapsed <= SENSOR_MOVE_WINDOW.total_seconds() / 60
            and abs(delta) >= SENSOR_MOVE_DELTA
            and mode is None
            and prev_mode is None
        ):
            # A large idle discontinuity is much more likely to be a moved
            # sensor than room physics. Keep learned values, but sharply lower
            # confidence and adapt faster for the next several observations.
            for estimate in (*self._rates.values(), *self._post_stop.values()):
                estimate.samples = min(estimate.samples, 2)
            self._fast_samples = 6
            self.sensor_move_count += 1
            self._settling = None
            self._last = (timestamp, temperature, mode, fan_mode)
            return

        alpha = FAST_LEARNING_ALPHA if self._fast_samples else LEARNING_ALPHA
        if self._fast_samples:
            self._fast_samples -= 1

        rate = delta / elapsed
        if prev_mode in ("cool", "heat") and mode == prev_mode:
            if (
                rate * _mode_direction(mode) > 0
                and MIN_VALID_RATE <= abs(rate) <= MAX_VALID_RATE
            ):
                self._update(self._rates, self._key(mode, prev_fan), rate, alpha)
                if outdoor_temperature is not None:
                    self._update(
                        self._rates,
                        self._key(mode, prev_fan, outdoor_temperature),
                        rate,
                        alpha,
                    )

        if prev_mode in ("cool", "heat") and mode is None:
            self._settling = (prev_mode, timestamp, temperature)
        elif self._settling is not None and mode is None:
            stopped_mode, stopped_at, stopped_temp = self._settling
            settle_minutes = (timestamp - stopped_at).total_seconds() / 60.0
            if 5 <= settle_minutes <= 30:
                drift = temperature - stopped_temp
                if drift * _mode_direction(stopped_mode) > 0 and abs(drift) <= 3.0:
                    drift_5m = drift * PROJECTION_MINUTES / settle_minutes
                    self._update(self._post_stop, stopped_mode, drift_5m, alpha)
                self._settling = None
            elif settle_minutes > 30:
                self._settling = None
        elif mode is not None:
            self._settling = None

        self._last = (timestamp, temperature, mode, fan_mode)

    def project(
        self,
        *,
        current: float,
        mode: str,
        fan_mode: str | None,
        live_slope: float | None,
        outdoor_temperature: float | None = None,
        minutes: float = PROJECTION_MINUTES,
    ) -> tuple[float, float]:
        """Return projected temperature and model confidence."""
        contextual = self._rates.get(self._key(mode, fan_mode, outdoor_temperature))
        estimate = contextual or self._rates.get(self._key(mode, fan_mode))
        confidence = estimate.confidence if estimate else 0.0
        learned_rate = estimate.rate if estimate else None

        if live_slope is None:
            # With no live trajectory, phase in learned behavior as evidence
            # accumulates instead of trusting one observation completely.
            rate = (learned_rate or 0.0) * confidence
        elif learned_rate is None:
            rate = live_slope
        else:
            # Live trajectory remains dominant until repeated cycles agree.
            learned_weight = 0.5 * confidence
            rate = live_slope * (1 - learned_weight) + learned_rate * learned_weight

        drift = self._post_stop.get(mode)
        post_stop = drift.rate * drift.confidence if drift else 0.0
        return current + rate * minutes + post_stop, confidence

    def as_dict(self) -> dict[str, Any]:
        """Return restart-safe, Home Assistant attribute-friendly state."""
        return {
            "version": 1,
            "rates": {
                key: {"rate": round(value.rate, 5), "samples": value.samples}
                for key, value in self._rates.items()
            },
            "post_stop": {
                key: {"drift": round(value.rate, 4), "samples": value.samples}
                for key, value in self._post_stop.items()
            },
            "sensor_move_count": self.sensor_move_count,
        }

    def restore(self, state: Any) -> None:
        """Restore validated estimates; silently ignore malformed old state."""
        if not isinstance(state, dict) or state.get("version") != 1:
            return
        for key, item in state.get("rates", {}).items():
            try:
                rate, samples = float(item["rate"]), int(item["samples"])
            except (KeyError, TypeError, ValueError):
                continue
            mode = str(key).partition(":")[0]
            if (
                mode in ("cool", "heat")
                and rate * _mode_direction(mode) > 0
                and MIN_VALID_RATE <= abs(rate) <= MAX_VALID_RATE
                and samples > 0
            ):
                self._rates[str(key)] = RateEstimate(rate, min(samples, 1000))
        for key, item in state.get("post_stop", {}).items():
            try:
                drift, samples = float(item["drift"]), int(item["samples"])
            except (KeyError, TypeError, ValueError):
                continue
            mode = str(key)
            if (
                mode in ("cool", "heat")
                and drift * _mode_direction(mode) > 0
                and abs(drift) <= 3.0
                and samples > 0
            ):
                self._post_stop[str(key)] = RateEstimate(drift, min(samples, 1000))
        try:
            self.sensor_move_count = max(0, int(state.get("sensor_move_count", 0)))
        except (TypeError, ValueError):
            pass


def gentle_setpoint_offset(
    error: float,
    configured_offset: float,
    *,
    gentle_error: float = 1.0,
    full_error: float = 3.0,
    gentle_offset: float = 2.0,
) -> float:
    """Return a smaller downstream offset as the room approaches target."""
    configured = max(0.0, configured_offset)
    gentle = min(configured, max(0.0, gentle_offset))
    if error <= gentle_error or full_error <= gentle_error:
        return gentle
    if error >= full_error:
        return configured
    fraction = (error - gentle_error) / (full_error - gentle_error)
    return gentle + fraction * (configured - gentle)


def projected_stop(
    *,
    cooling: bool,
    current: float,
    projected: float,
    target: float,
    anticipation: float = 0.1,
) -> bool:
    """Stop at target, or slightly early when the trajectory will cross it."""
    if cooling:
        return current <= target or (
            projected < current and projected <= target + anticipation
        )
    return current >= target or (
        projected > current and projected >= target - anticipation
    )
