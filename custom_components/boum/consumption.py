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
    the span.  Where exactly comes from the pump log in _attribute_by_day.
    """
    span = hours[start:end]
    if not span:
        return
    share = total / len(span)
    for hour in span:
        usage[hour] += share


def _attribute_by_day(
    loss_by_hour: dict[datetime, float],
    pumped_by_hour: dict[datetime, float],
    first_hour: datetime,
    last_hour: datetime,
) -> dict[datetime, float]:
    """Split the measured level loss into pumped water and a background rate.

    The tank loses water through two very differently timed processes, and only
    one of them is observable per hour:

      * The pump reports both the volume and the moment, to the second.  That
        water is booked on its own hour.
      * Evaporation and seepage run continuously and are far too slow to break
        the deadband within an hour.  Their timing is simply not in the data,
        so the remainder of a day's loss is spread evenly over that day.

    Grouping per day rather than per drop keeps the daily totals — the figures
    the forecast trains on — exactly as measured, while giving every hour a
    non-zero background share.  A day ends up at max(measured loss, pumped),
    so consumption can never come out below what the pump moved, per hour and
    per day alike.
    """
    hours_by_day: defaultdict[date, list[datetime]] = defaultdict(list)
    for hour in loss_by_hour:
        hours_by_day[hour.date()].append(hour)
    # Hours the pump reports but the level series has no reading for: one the
    # device slept through, or the newest complete hour, which has no successor
    # and can therefore never carry a level drop.
    for hour in pumped_by_hour:
        if first_hour <= hour <= last_hour and hour not in loss_by_hour:
            hours_by_day[hour.date()].append(hour)

    usage: dict[datetime, float] = {}
    for day_hours in hours_by_day.values():
        day_loss = sum(loss_by_hour.get(hour, 0.0) for hour in day_hours)
        day_pumped = sum(pumped_by_hour.get(hour, 0.0) for hour in day_hours)
        background = max(0.0, day_loss - day_pumped) / len(day_hours)
        for hour in day_hours:
            usage[hour] = pumped_by_hour.get(hour, 0.0) + background
    return usage


def calculate_water_usage_from_level(
    readings: list[tuple[datetime, float]],
    *,
    pumped_by_hour: dict[datetime, float] | None = None,
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
         consumption booked earlier.
      4. Each confirmed drop is spread evenly over the hours it spans, which
         establishes how much the tank lost on each day.
      5. That daily loss is then attributed: the pump log places its volumes on
         the exact hours they ran in, the rest becomes an even background rate
         for the day (see _attribute_by_day).

    Because inflow and outflow are processed in sequence rather than netted
    against each other, slow rain does not mask real consumption.

    Parameters
    ----------
    readings:
        List of (hour, water_level_liters) pairs.
    pumped_by_hour:
        Optional per-hour pump volumes.  Used both to time the pumped share of
        a confirmed drop and as a hard lower bound per hour, so water the pump
        demonstrably moved counts as consumed even when simultaneous inflow
        kept the level flat.  The caller is responsible for passing only
        complete hours.
    deadband_l:
        Half-width of the band around the baseline that is treated as sensor
        artefact rather than a real level change.
    smoothing_hours:
        Window of the rolling median applied before tracking.
    """
    if len(readings) < 2:
        return {}

    pumped = pumped_by_hour or {}
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
        # Both a confirmed drop and confirmed inflow re-anchor the baseline.
        baseline = levels[i]
        anchor = i

    return _attribute_by_day(loss, pumped, hours[0], hours[-1])


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
