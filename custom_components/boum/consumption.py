"""Water consumption calculations for the Boum integration.

Two methods are provided:

  calculate_water_usage_from_level  — tank-drop based, baseline tracking
  calculate_water_pumped_from_log   — exact volume from pumpStopped log events
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from datetime import date, datetime


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


def _spread_drop(
    usage: dict[datetime, float],
    hours: list[datetime],
    start: int,
    end: int,
    total: float,
) -> None:
    """Book *total* litres evenly across hours[start:end].

    The previous version weighted the drop by the raw per-hour steps, which
    gave weight 0 — and therefore a hard zero — to every hour whose smoothed
    level happened to read flat or minimally up.  With slow consumption that is
    most hours, and it was the main source of the isolated zeros in the hourly
    series.  Those steps are noise at this scale anyway; what the tracker
    actually establishes is that *total* litres left the tank somewhere within
    the span, not which hour of it.
    """
    span = hours[start:end]
    if not span:
        return
    share = total / len(span)
    for hour in span:
        usage[hour] += share


def _smooth_over_days(
    loss_by_hour: dict[datetime, float],
) -> dict[datetime, float]:
    """Flatten each day's measured loss into an even hourly rate.

    Evaporation, seepage and irrigation all leave the tank far too slowly to
    break the deadband inside a single hour, so the hour-level timing of a loss
    is not in the level data to begin with.  What the tracker does establish
    reliably is the total per day.  Spreading that total evenly over the day
    keeps the daily figures — the ones the forecast trains on — exactly as
    measured, and gives every hour a share instead of leaving the hours between
    two confirmed drops at zero.

    The pump log is deliberately not consulted here.  It would supply exact
    timing, but its volumes cannot be trusted (the API reports them far too
    high), and anything derived from them would pull the measured consumption
    with it.  Water Usage is therefore purely what the tank level shows.
    """
    hours_by_day: defaultdict[date, list[datetime]] = defaultdict(list)
    for hour in loss_by_hour:
        hours_by_day[hour.date()].append(hour)

    usage: dict[datetime, float] = {}
    for day_hours in hours_by_day.values():
        share = sum(loss_by_hour[hour] for hour in day_hours) / len(day_hours)
        for hour in day_hours:
            usage[hour] = share
    return usage


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
      4. Each confirmed drop is spread evenly over the hours it spans, which
         establishes how much the tank lost on each day.
      5. That daily loss is flattened into an even hourly rate for the day, so
         no hour is left at zero just because it sits between two confirmed
         drops (see _smooth_over_days).

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

    baseline = levels[0]
    anchor = 0
    for i in range(1, len(levels)):
        delta = baseline - levels[i]
        if delta > deadband_l:  # confirmed outflow
            _spread_drop(loss, hours, anchor, i, delta)
        elif -delta <= deadband_l:  # inside the band — sensor artefact
            continue
        else:  # confirmed inflow — rain through the lid, or a refill
            # Settle what the level had already lost before the inflow
            # arrived.  Re-anchoring upwards otherwise discards the
            # sub-deadband amount still pending at that moment, so a refill
            # silently erased up to a full deadband of real consumption.
            pending = baseline - levels[i - 1]
            if pending > 0.0:
                _spread_drop(loss, hours, anchor, i - 1, pending)
        # Both a confirmed drop and confirmed inflow re-anchor the baseline.
        baseline = levels[i]
        anchor = i

    return _smooth_over_days(loss)


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
