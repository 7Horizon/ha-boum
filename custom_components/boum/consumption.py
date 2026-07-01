"""Water consumption calculations for the Boum integration.

Two methods are provided:

  calculate_water_usage_from_level  — tank-drop based (ported from Boum app TS)
  calculate_water_pumped_from_log   — exact volume from pumpStopped log events
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime


def calculate_water_usage_from_level(
    readings: list[tuple[datetime, float]],
    *,
    noise_gate_l: float = 0.5,
) -> dict[datetime, float]:
    """Return per-hour water consumption (L) from consecutive tank-level readings.

    Port of *calculateWaterConsumptionInLiters* from the Boum app.

    Original logic (per-minute resolution):
      1. If the total spread of the dataset is < 0.5 L → treat as noise, return 0.
      2. For each consecutive pair: if |diff| > 0.5 L → skip (spike); else add diff.
      3. Return abs(total).

    Adaptation for hourly resolution:
      - Step 1 (noise gate) is kept as-is: negligible overall variation means no
        real consumption occurred.
      - Step 2 spike filter is handled *upstream* by filter_level_spikes() before
        this function is called, so only sane readings arrive here.  All remaining
        positive drops count as genuine consumption.

    Parameters
    ----------
    readings:
        List of (timestamp, water_level_liters) pairs — already spike-filtered.
    noise_gate_l:
        Minimum total level variation (L) required before any consumption is
        counted.  Below this threshold the whole period is treated as sensor
        drift and 0 is returned for every hour.
    """
    if len(readings) < 2:
        return {}

    pts = sorted(readings, key=lambda r: r[0])
    levels = [v for _, v in pts]

    # Noise gate: if the total spread is negligible, treat the whole period as drift.
    if max(levels) - min(levels) < noise_gate_l:
        return {ts: 0.0 for ts, _ in pts[:-1]}

    return {
        pts[i][0]: max(0.0, pts[i][1] - pts[i + 1][1])
        for i in range(len(pts) - 1)
    }


def calculate_water_pumped_from_log(
    log_entries: list[dict],
    *,
    since: datetime | None = None,
) -> dict[datetime, float]:
    """Return per-hour water pumped (L) from device log pumpStopped events.

    The device firmware emits a pumpStopped log entry after each pump cycle
    containing the exact measured volume (payload.totalPumpedVolume, in L).
    This is more accurate than the flow-rate estimate because it accounts for
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
    for entry in log_entries:
        if entry.get("type") != "pumpStopped":
            continue
        volume = entry.get("payload.totalPumpedVolume")
        if volume is None:
            continue
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            continue
        raw_ts = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if since is not None and ts < since:
            continue
        hour = ts.replace(minute=0, second=0, microsecond=0)
        result[hour] += volume
    return dict(result)
