"""Push Boum telemetry history into HA long-term statistics."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from .const import DEFAULT_DEVICE_MODEL, DEFAULT_TANK_TYPE, DOMAIN
from .tank import water_level_liters as _tank_wl

_LOGGER = logging.getLogger(__name__)

# StatisticMeanType exists on newer HA cores; older ones use has_mean instead.
try:
    from homeassistant.components.recorder.statistics import StatisticMeanType

    _MEAN_KWARGS: dict = {"mean_type": StatisticMeanType.ARITHMETIC}
except ImportError:
    _MEAN_KWARGS = {"has_mean": True}


# All telemetry fields to import as statistics.
# (api_key, statistic_id_suffix, display_name, unit)
# The water_level series is converted with the tank-specific formula at call time.
_STAT_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("waterTableRange", "water_level",      "Water Level",      "L"),
    ("temperature",     "temperature",      "Temperature",      "°C"),
    ("temperatureEsp",  "temperature_esp",  "ESP Temperature",  "°C"),
    ("batteryCapacity", "battery_capacity", "Battery Capacity", "%"),
    ("batteryVoltage",  "battery_voltage",  "Battery Voltage",  "V"),
    ("batteryCurrent",  "battery_current",  "Battery Current",  "A"),
    ("solarVoltage",    "solar_voltage",    "Solar Voltage",    "V"),
    ("inputCurrent",    "input_current",    "Input Current",    "A"),
    ("wifiStrength",    "wifi_strength",    "Wi-Fi Strength",   "dBm"),
)


def _iqr_filter_low(vals: list[float]) -> list[float]:
    """Remove low outliers via IQR method — catches lid-open sensor artefacts.

    Only applied when ≥ 5 readings are in a bucket so the quartiles are meaningful.
    Falls back to the original list if filtering would remove too many values.
    """
    s = sorted(vals)
    n = len(s)
    q1, q3 = s[n // 4], s[3 * n // 4]
    fence = q1 - 1.5 * (q3 - q1)
    filtered = [v for v in vals if v >= fence]
    return filtered if len(filtered) >= n // 2 else vals


def _parse_point(point: dict) -> tuple[datetime | None, float | None]:
    raw_ts = point.get("x", "")
    raw_val = point.get("y")
    try:
        ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None, None
    try:
        val = float(raw_val) if raw_val is not None else None
    except (TypeError, ValueError):
        val = None
    return ts, val


def _build_hourly_stats(
    series: list[dict],
    transform: Callable[[float], float] | None,
    *,
    filter_low_outliers: bool = False,
) -> list[StatisticData]:
    """Aggregate {x, y} API points into hourly mean/min/max StatisticData objects."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for point in series:
        ts, val = _parse_point(point)
        if ts is None or val is None:
            continue
        if transform is not None:
            val = transform(val)
        hour = ts.replace(minute=0, second=0, microsecond=0)
        buckets[hour].append(val)

    result = []
    for hour, vals in sorted(buckets.items()):
        if filter_low_outliers and len(vals) >= 5:
            vals = _iqr_filter_low(vals)
        result.append(
            StatisticData(
                start=hour,
                mean=sum(vals) / len(vals),
                min=min(vals),
                max=max(vals),
            )
        )
    return result


async def _async_last_sum_before(
    hass: HomeAssistant, stat_id: str, first_hour: datetime
) -> float:
    """Return the cumulative sum of the newest statistics row before *first_hour*.

    Summing stats are rewritten in batches (incremental log imports, rolling
    level windows).  HA renders period totals as differences of the cumulative
    sum, so each batch must continue where the preceding row left off —
    restarting at 0 would produce negative changes at every batch boundary.
    """
    start = first_hour - timedelta(days=30)
    end = first_hour - timedelta(seconds=1)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    for row in reversed(stats.get(stat_id, [])):
        s = row.get("sum") if isinstance(row, dict) else row.sum
        if s is not None:
            return float(s)
    return 0.0


async def _async_write_summing_stat(
    hass: HomeAssistant,
    stat_id: str,
    name: str,
    data_by_hour: dict,
    unit: str = "L",
) -> None:
    """Write hourly summing statistics to the HA recorder.

    Each entry carries mean=val, min=val, max=val and a cumulative sum so the
    HA statistics card can show both instantaneous and total (Sum) views.
    data_by_hour maps hour-start datetime → value for that hour (≥ 0).
    """
    if not data_by_hour:
        return

    running_sum = await _async_last_sum_before(hass, stat_id, min(data_by_hour))
    stat_data = []
    for hour, val in sorted(data_by_hour.items()):
        running_sum += val
        stat_data.append(
            StatisticData(start=hour, mean=val, min=val, max=val, sum=running_sum)
        )

    meta = StatisticMetaData(
        **_MEAN_KWARGS,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=stat_id,
        unit_of_measurement=unit,
    )
    async_add_external_statistics(hass, meta, stat_data)
    _LOGGER.debug("Imported %d buckets → %s", len(stat_data), stat_id)


async def import_water_usage_statistics(
    hass: HomeAssistant,
    device_id: str,
    usage_by_hour: dict,
) -> None:
    """Write pre-computed hourly water usage (L) to HA long-term statistics.

    usage_by_hour maps hour-start datetime → litres consumed that hour (≥ 0).
    Derived from tank level drops via baseline tracking (see consumption.py).
    """
    short_id = device_id[:8]
    await _async_write_summing_stat(
        hass,
        stat_id=f"{DOMAIN}:{short_id}_water_usage",
        name=f"Boum {short_id} Water Usage",
        data_by_hour=usage_by_hour,
    )


async def import_water_pumped_statistics(
    hass: HomeAssistant,
    device_id: str,
    pumped_by_hour: dict,
) -> None:
    """Write pre-computed hourly pump volume (L) to HA long-term statistics.

    pumped_by_hour maps hour-start datetime → litres pumped that hour (≥ 0).
    Derived from pumpStopped log events (payload.totalPumpedVolume), summed per hour.
    """
    short_id = device_id[:8]
    await _async_write_summing_stat(
        hass,
        stat_id=f"{DOMAIN}:{short_id}_water_pumped",
        name=f"Boum {short_id} Water Pumped",
        data_by_hour=pumped_by_hour,
    )


def import_statistics(
    hass: HomeAssistant,
    device_id: str,
    hourly_telemetry: dict,
    tank_type: str = DEFAULT_TANK_TYPE,
    device_model: str = DEFAULT_DEVICE_MODEL,
) -> None:
    """Import hourly telemetry as HA long-term statistics.

    The caller is responsible for passing only the data that needs importing
    (incremental fetch). All points in hourly_telemetry are pushed.
    """
    wl_transform: Callable[[float], float] = lambda x: _tank_wl(
        x, tank_type, device_model
    )

    time_series = hourly_telemetry.get("timeSeries", {})
    short_id = device_id[:8]
    imported = 0

    for api_key, id_suffix, display_name, unit in _STAT_FIELDS:
        series = time_series.get(api_key, [])
        if not series:
            _LOGGER.debug("No %s data for device %s, skipping statistic", api_key, short_id)
            continue
        stats = _build_hourly_stats(
            series,
            wl_transform if id_suffix == "water_level" else None,
            filter_low_outliers=(id_suffix == "water_level"),
        )
        if not stats:
            continue
        meta = StatisticMetaData(
            **_MEAN_KWARGS,
            has_sum=False,
            name=f"Boum {short_id} {display_name}",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:{short_id}_{id_suffix}",
            unit_of_measurement=unit,
        )
        async_add_external_statistics(hass, meta, stats)
        imported += 1
        _LOGGER.debug("Imported %d buckets → %s:%s_%s", len(stats), DOMAIN, short_id, id_suffix)

    _LOGGER.info(
        "Boum statistics import complete for %s: %d/%d fields written",
        short_id,
        imported,
        len(_STAT_FIELDS),
    )
