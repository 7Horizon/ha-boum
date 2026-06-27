"""Push Boum telemetry history into HA long-term statistics."""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime

from homeassistant.core import HomeAssistant

from .const import DEFAULT_DEVICE_MODEL, DEFAULT_TANK_TYPE, DOMAIN
from .tank import water_level_liters as _tank_wl

_LOGGER = logging.getLogger(__name__)


# All telemetry fields to import as statistics.
# (api_key, statistic_id_suffix, display_name, unit, optional_transform)
# The water_level transform is overridden at call-time with the tank-specific formula.
_STAT_FIELDS: tuple[tuple[str, str, str, str, Callable[[float], float] | None], ...] = (
    ("flowRate",        "flow_rate",          "Flow Rate",          "L/min",  None),
    ("flowRate",        "hourly_consumption",  "Hourly Consumption", "L/h",    lambda x: x * 60.0),
    ("waterTableRange", "water_level",         "Water Level",        "L",      None),  # overridden below
    ("temperature",     "temperature",         "Temperature",        "°C",     None),
    ("temperatureEsp",  "temperature_esp",     "ESP Temperature",    "°C",     None),
    ("batteryCapacity", "battery_capacity",    "Battery Capacity",   "%",      None),
    ("batteryVoltage",  "battery_voltage",     "Battery Voltage",    "V",      None),
    ("batteryCurrent",  "battery_current",     "Battery Current",    "A",      None),
    ("solarVoltage",    "solar_voltage",       "Solar Voltage",      "V",      None),
    ("inputCurrent",    "input_current",       "Input Current",      "A",      None),
    ("wifiStrength",    "wifi_strength",       "Wi-Fi Strength",     "dBm",    None),
)


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
    StatisticData,
    transform: Callable[[float], float] | None,
) -> list:
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

    return [
        StatisticData(
            start=hour,
            mean=sum(vals) / len(vals),
            min=min(vals),
            max=max(vals),
        )
        for hour, vals in sorted(buckets.items())
    ]


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
    try:
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )
    except ImportError:
        _LOGGER.warning("homeassistant.components.recorder.statistics not found; skipping")
        return

    StatisticData = StatisticMetaData = None
    for mod in (
        "homeassistant.components.recorder.statistics",
        "homeassistant.components.recorder.models",
    ):
        try:
            import importlib
            m = importlib.import_module(mod)
            StatisticData = getattr(m, "StatisticData", None)
            StatisticMetaData = getattr(m, "StatisticMetaData", None)
            if StatisticData and StatisticMetaData:
                break
        except ImportError:
            continue

    if StatisticData is None or StatisticMetaData is None:
        _LOGGER.warning("StatisticData/StatisticMetaData not found; skipping import")
        return

    try:
        from homeassistant.components.recorder.statistics import StatisticMeanType
        mean_kwargs: dict = {"mean_type": StatisticMeanType.ARITHMETIC}
    except ImportError:
        mean_kwargs = {"has_mean": True}

    # Build tank-specific water level transform once for this import call.
    wl_transform: Callable[[float], float] = lambda x: _tank_wl(x, tank_type, device_model)

    time_series = hourly_telemetry.get("timeSeries", {})
    short_id = device_id[:8]
    imported = 0

    for api_key, id_suffix, display_name, unit, transform in _STAT_FIELDS:
        effective_transform = wl_transform if id_suffix == "water_level" else transform
        series = time_series.get(api_key, [])
        if not series:
            _LOGGER.debug("No %s data for device %s, skipping statistic", api_key, short_id)
            continue
        stats = _build_hourly_stats(series, StatisticData, effective_transform)
        if not stats:
            continue
        meta = StatisticMetaData(
            **mean_kwargs,
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
