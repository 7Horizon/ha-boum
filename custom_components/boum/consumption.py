"""Water consumption calculations for the Boum integration.

Two methods are provided:

  calculate_water_usage_from_level  — tank-drop based, baseline tracking
  calculate_water_pumped_from_log   — exact volume from pumpStopped log events
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime


def _rolling_median(values: list[float], window: int) -> list[float]:
    """Centred rolling median; the window shrinks towards both ends."""
    if window < 2 or len(values) < 2:
        return list(values)
    half = window // 2
    out: list[float] = []
    for i in range(len(values)):
        chunk = sorted(values[max(0, i - half) : i + half + 1])
        mid = len(chunk) // 2
        out.append(
            chunk[mid] if len(chunk) % 2 else (chunk[mid - 1] + chunk[mid]) / 2
        )
    return out


_BACKGROUND_SHARE = 0.15
"""Fraction of a confirmed drop treated as continuous background loss.

Sized to roughly the evaporation share of a typical span: with one irrigation
per day and evaporation far below the deadband, a span covers about a litre of
irrigation plus a tenth of a litre of evaporation.  Its only job is to keep
every hour of a span above zero — the rest follows the level steps.
"""


def _spread_drop(
    loss: dict[datetime, float],
    hours: list[datetime],
    levels: list[float],
    start: int,
    end: int,
    total: float,
) -> None:
    """Book *total* litres across hours[start:end], shaped by the level steps.

    A span is not homogeneous.  It typically holds one irrigation, which the
    level records as a sharp step in a single hour, plus evaporation running
    continuously underneath.  The per-hour steps carry exactly that
    distinction, so most of the drop follows them.

    Spreading the whole drop evenly instead — as an earlier version did —
    smeared each irrigation backwards across every hour since the previous one.
    Because evaporation never breaks the deadband, that anchor is a full day
    back, so a single watering was spread over the preceding 24 hours and the
    rolling 24 h sensor sawtoothed by a factor of five.

    A fixed _BACKGROUND_SHARE is still spread evenly, which is both physically
    right for the evaporation part and enough to keep hours whose step reads
    flat from landing on exactly zero.
    """
    span = hours[start:end]
    if not span:
        return

    steps = [max(0.0, levels[j] - levels[j + 1]) for j in range(start, end)]
    weight = sum(steps)
    background = total * _BACKGROUND_SHARE / len(span)
    shaped = total - total * _BACKGROUND_SHARE

    for offset, hour in enumerate(span):
        loss[hour] += background + (
            shaped * steps[offset] / weight if weight > 0 else shaped / len(span)
        )


def calculate_water_usage_from_level(
    readings: list[tuple[datetime, float]],
    *,
    deadband_l: float = 0.3,
    smoothing_hours: int = 3,
) -> dict[datetime, float]:
    """Return per-hour water consumption (L) from consecutive tank-level readings.

    Plain hour-to-hour differencing is unusable here: summing only the downward
    steps rectifies every fluctuation that comes back into phantom consumption.
    The dominant source is not random noise but the diurnal drift of the
    ultrasonic reading with air temperature, which alone accounts for roughly
    half a litre a day.

    The level is therefore tracked against a confirmed baseline:

      1. A rolling median (*smoothing_hours*) removes single-hour outliers,
         including the lid-open artefact.
      2. Movements within ±*deadband_l* of the baseline are ignored — this is
         where noise and thermal drift end up.
      3. A drop beyond the deadband is booked as consumption and pulls the
         baseline down.  A rise beyond it is inflow (rain entering through the
         lid, or a refill): it only moves the baseline up and never cancels
         consumption booked earlier.  Whatever the level had lost before the
         inflow arrived is settled first, so re-anchoring upwards cannot
         swallow consumption that was still pending under the deadband.
      4. Each confirmed drop is booked across the hours it spans, shaped by the
         per-hour level steps so an irrigation lands on the hour it happened
         in, with a small even background share (see _spread_drop).
      5. Whatever is still pending when the readings run out is settled too,
         so the newest hours are not left empty until the next confirmation.

    Because inflow and outflow are processed in sequence rather than netted
    against each other, slow rain does not mask real consumption.

    Parameters
    ----------
    readings:
        List of (hour, water_level_liters) pairs.
    deadband_l:
        Half-width of the band around the baseline that is treated as sensor
        artefact rather than a real level change.
    smoothing_hours:
        Window of the rolling median applied before tracking.
    """
    if len(readings) < 2:
        return {}

    pts = sorted(readings, key=lambda r: r[0])
    hours = [ts for ts, _ in pts]
    levels = _rolling_median([v for _, v in pts], smoothing_hours)

    # The newest hour has no successor, so it can never carry a drop.
    loss: dict[datetime, float] = {ts: 0.0 for ts in hours[:-1]}
    last = len(levels) - 1

    baseline = levels[0]
    anchor = 0
    for i in range(1, len(levels)):
        delta = baseline - levels[i]
        if delta > deadband_l:  # confirmed outflow
            _spread_drop(loss, hours, levels, anchor, i, delta)
        elif -delta <= deadband_l:  # inside the band — sensor artefact
            continue
        else:  # confirmed inflow — rain through the lid, or a refill
            # Settle what the level had already lost before the inflow
            # arrived.  Re-anchoring upwards otherwise discards the
            # sub-deadband amount still pending at that moment, so a refill
            # silently erased up to a full deadband of real consumption.
            pending = baseline - levels[i - 1]
            if pending > 0.0:
                _spread_drop(loss, hours, levels, anchor, i - 1, pending)
        # Both a confirmed drop and confirmed inflow re-anchor the baseline.
        baseline = levels[i]
        anchor = i

    # Settle the tail.  Consumption after the last confirmation used to sit
    # unbooked until the next one, which left the newest hours empty and made
    # the 24 h sensor drop out for an hour a day, right after watering.
    pending = baseline - levels[last]
    if pending > 0.0:
        _spread_drop(loss, hours, levels, anchor, last, pending)

    return loss


def iter_pump_events(log_entries: list[dict]) -> Iterator[tuple[datetime, float]]:
    """Yield (timestamp, volume_l) for every pumpStopped entry in a device log.

    Entries with a missing or unparsable timestamp/volume are skipped.
    """
    for entry in log_entries:
        if entry.get("type") != "pumpStopped":
            continue
        try:
            volume = float(entry["payload.totalPumpedVolume"])
            ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        yield ts, volume


def calculate_water_pumped_from_log(
    log_entries: list[dict],
    *,
    since: datetime | None = None,
) -> dict[datetime, float]:
    """Return per-hour water pumped (L) from device log pumpStopped events.

    The device firmware emits a pumpStopped log entry after each pump cycle
    containing the exact measured volume (payload.totalPumpedVolume, in L).
    This is more accurate than a flow-rate estimate because it accounts for
    the pump switch-off lag and uses the device's own measurement.

    Multiple pump cycles within the same hour are summed into a single bucket.

    Parameters
    ----------
    log_entries:
        Raw list of log entry dicts from GET /devices/{id}/log.
    since:
        If given, ignore entries with a timestamp before this value.  Use the
        last known water_pumped stat timestamp to avoid reprocessing old data.
    """
    result: defaultdict[datetime, float] = defaultdict(float)
    for ts, volume in iter_pump_events(log_entries):
        if since is not None and ts < since:
            continue
        result[ts.replace(minute=0, second=0, microsecond=0)] += volume
    return dict(result)
