"""DataUpdateCoordinator for the Boum integration."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BoumApi, BoumApiError
from .const import (
    CONF_DEVICE_MODEL,
    CONF_DEVICES,
    CONF_TANK_TYPE,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_TANK_TYPE,
    DOMAIN,
    MINUTELY_HOURS,
    SENSOR_STATS_HOURS,
    STATISTICS_BACKFILL_DAYS,
    UPDATE_INTERVAL,
    WEATHER_ENTITY,
)
from .consumption import (
    calculate_water_pumped_from_log,
    calculate_water_usage_from_level,
    iter_pump_events,
)
from .prediction import DayForecast, PredictionResult, compute_prediction
from .statistics import import_statistics, import_water_pumped_statistics, import_water_usage_statistics
from .tank import water_level_liters

_LOGGER = logging.getLogger(__name__)


def _to_datetime(ts) -> datetime | None:
    """Coerce a statistics row timestamp to an aware datetime.

    HA returns different types depending on version: datetime, float (Unix
    epoch), int, or ISO-format string.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _compute_hourly_usage(
    water_level: dict[datetime, float],
) -> dict[datetime, float]:
    """Return hourly water consumption (L) using the Boum level-based algorithm.

    Applies filter_level_spikes first (lid-open artefacts), then delegates to
    calculate_water_usage_from_level which mirrors the original Boum app logic:
    noise gate + per-hour drop accumulation.
    """
    if len(water_level) < 2:
        return {}
    points = sorted(water_level.items(), key=lambda p: p[0])
    points = filter_level_spikes(points)
    return calculate_water_usage_from_level(points)


def filter_level_spikes(
    points: list[tuple[datetime, float]],
    min_drop: float = 1.0,
) -> list[tuple[datetime, float]]:
    """Replace spike readings caused by the ultrasonic sensor seeing the open lid.

    A spike is detected when the level drops by at least *min_drop* litres AND the
    next reading recovers by at least 50 % of that drop.  The spike value is
    replaced with the average of its two neighbours so that drop-based consumption
    calculations ignore the artefact.  The recovery condition makes this safe: real
    consumption (pump cycles) does not recover, so genuine usage is never removed.
    Handles single-hour spikes only; multi-hour lid-open periods are covered by the
    per-minute IQR filter in statistics.py.
    """
    if len(points) < 3:
        return points
    result = [points[0]]
    for i in range(1, len(points) - 1):
        prev_val = result[-1][1]
        curr_ts, curr_val = points[i]
        next_val = points[i + 1][1]
        drop = prev_val - curr_val
        recovery = next_val - curr_val
        if drop >= min_drop and recovery >= drop * 0.5:
            result.append((curr_ts, (prev_val + next_val) / 2))
        else:
            result.append(points[i])
    result.append(points[-1])
    return result


def _last_pump_from_log(log_entries: list[dict]) -> datetime | None:
    """Return the exact timestamp of the most recent pumpStopped log event."""
    return max((ts for ts, _ in iter_pump_events(log_entries)), default=None)


def latest_value(time_series: dict, key: str) -> float | None:
    """Return the most recent non-null y value for a timeSeries key."""
    for point in reversed(time_series.get(key, [])):
        v = point.get("y")
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def current_level(device_data: dict, tank_type: str, device_model: str) -> float | None:
    """Return current water level in litres from coordinator device data."""
    for data_key in ("minutely", "hourly"):
        raw = latest_value(
            device_data.get(data_key, {}).get("timeSeries", {}), "waterTableRange"
        )
        if raw is not None:
            return water_level_liters(raw, tank_type, device_model)
    return None


def _stat_rows(stats: dict, stat_id: str):
    """Yield (timestamp, mean) pairs from a statistics_during_period result."""
    for row in stats.get(stat_id, []):
        mean = row.get("mean") if isinstance(row, dict) else row.mean
        ts = _to_datetime(row.get("start") if isinstance(row, dict) else row.start)
        if ts is not None and mean is not None:
            yield ts, float(mean)


def _coerce_float(*candidates: object, default: float) -> float:
    """Return the first candidate convertible to float; None is skipped.

    Unlike an `or` chain this keeps legitimate zero values (0 °C, 0 mm).
    """
    for v in candidates:
        if v is None:
            continue
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return default


class BoumCoordinator(DataUpdateCoordinator[dict]):
    """Fetch data for all claimed Boum devices."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, api: BoumApi, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.api = api
        self.config_entry = entry

    def tank_type(self, device_id: str) -> str:
        """Configured tank type for a device.

        Falls back to the legacy account-wide option (pre multi-device
        installs), then to the default.
        """
        per_device = self.config_entry.options.get(CONF_DEVICES, {}).get(device_id, {})
        return per_device.get(CONF_TANK_TYPE) or self.config_entry.options.get(
            CONF_TANK_TYPE, DEFAULT_TANK_TYPE
        )

    def device_model(self, device_id: str) -> str:
        """Configured controller model for a device (same fallback chain as tank_type)."""
        per_device = self.config_entry.options.get(CONF_DEVICES, {}).get(device_id, {})
        return per_device.get(CONF_DEVICE_MODEL) or self.config_entry.options.get(
            CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL
        )

    async def _async_update_data(self) -> dict:
        try:
            devices = await self.api.get_claimed_devices()
        except BoumApiError as err:
            raise UpdateFailed(f"Could not fetch device list: {err}") from err

        # Only poll devices the user has configured (tank/controller known).
        # Legacy entries without a devices dict keep polling everything.
        configured = self.config_entry.options.get(CONF_DEVICES)
        if configured is not None:
            devices = [d for d in devices if d["id"] in configured]

        if not devices:
            _LOGGER.warning("No configured Boum devices found for this account")
            return {}

        now = datetime.now(timezone.utc)
        minutely_start = now - timedelta(hours=MINUTELY_HOURS)

        result: dict = {}
        for device in devices:
            device_id = device["id"]
            device_name = device["name"] or f"Boum {device_id[:8]}"
            try:
                state = await self.api.get_device_state(device_id)
                minutely = await self.api.get_device_telemetry(
                    device_id, minutely_start, now, interval="60s"
                )

                # Fetch sensor data from HA statistics (no API needed).
                # Also determines the incremental hourly fetch window.
                hass_stats = await self._async_get_hourly_stats_for_sensors(device_id)

                # Incremental hourly fetch: only since the last known stat timestamp.
                # Falls back to a full backfill window on first install.
                if hass_stats.get("water_level"):
                    last_stat_ts = max(hass_stats["water_level"].keys())
                    hourly_start = last_stat_ts - timedelta(hours=1)
                else:
                    hourly_start = now - timedelta(days=STATISTICS_BACKFILL_DAYS)

                hourly = await self.api.get_device_telemetry(
                    device_id, hourly_start, now, interval="3600s"
                )
                _LOGGER.debug(
                    "Device %s: hourly fetch from %s (%d points)",
                    device_id[:8],
                    hourly_start.isoformat(),
                    sum(
                        len(v)
                        for v in hourly.get("timeSeries", {}).values()
                    ),
                )

                device_log = await self.api.get_device_log(device_id)

                # Last irrigation: exact timestamp from pumpStopped log event.
                # Falls back to water_pumped statistics (60-day window) when
                # the log window does not cover the most recent pump cycle.
                last_irrigation = _last_pump_from_log(device_log)
                if last_irrigation is None:
                    last_irrigation = await self._async_get_last_irrigation(device_id)

                result[device_id] = {
                    "name": device_name,
                    "state": state,
                    "minutely": minutely,
                    "hourly": hourly,
                    "last_irrigation": last_irrigation,
                    "hass_stats": hass_stats,
                }
                try:
                    import_statistics(
                        self.hass,
                        device_id,
                        hourly,
                        self.tank_type(device_id),
                        self.device_model(device_id),
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Statistics import failed for %s: %s", device_id, err)

                try:
                    usage_by_hour = _compute_hourly_usage(hass_stats.get("water_level", {}))
                    await import_water_usage_statistics(self.hass, device_id, usage_by_hour)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Water usage statistics import failed for %s: %s", device_id, err)

                try:
                    pumped_stats = hass_stats.get("water_pumped", {})
                    since = (max(pumped_stats.keys()) if pumped_stats else None)
                    pumped_by_hour = calculate_water_pumped_from_log(device_log, since=since)
                    await import_water_pumped_statistics(self.hass, device_id, pumped_by_hour)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Water pumped statistics import failed for %s: %s", device_id, err)
            except BoumApiError as err:
                raise UpdateFailed(
                    f"Error fetching data for device {device_id}: {err}"
                ) from err

        try:
            forecasts = await self._async_build_forecasts(result)
            for device_id, forecast in forecasts.items():
                result[device_id]["forecast"] = forecast
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Forecast computation failed: %s", err)

        return result

    # ------------------------------------------------------------------
    # HA statistics helpers
    # ------------------------------------------------------------------

    async def _async_stats_during_period(
        self, start: datetime, stat_ids: set[str]
    ) -> dict:
        """Run an hourly mean statistics_during_period query in the executor."""
        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            None,
            stat_ids,
            "hour",
            None,
            {"mean"},
        )

    async def _async_get_hourly_stats_for_sensors(
        self, device_id: str
    ) -> dict[str, dict[datetime, float]]:
        """Fetch water_pumped, water_level, and water_usage from HA statistics.

        Returns data for the last SENSOR_STATS_HOURS hours (enough for the
        3-day average sensor and 24 h daily totals). The most recent water_level
        timestamp drives the incremental hourly API fetch window.
        """
        short_id = device_id[:8]
        keys = ("water_pumped", "water_level", "water_usage")
        start = datetime.now(timezone.utc) - timedelta(hours=SENSOR_STATS_HOURS)

        try:
            stats = await self._async_stats_during_period(
                start, {f"{DOMAIN}:{short_id}_{key}" for key in keys}
            )
            return {
                key: dict(_stat_rows(stats, f"{DOMAIN}:{short_id}_{key}"))
                for key in keys
            }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read HA statistics for sensors: %s", err)
            return {}

    async def _async_get_last_irrigation(self, device_id: str) -> datetime | None:
        """Return last irrigation timestamp from water_pumped statistics (60-day window).

        Fallback for when the device log does not cover the most recent pump
        cycle (log window is typically ~24 h).  Hourly precision only.
        """
        stat_id = f"{DOMAIN}:{device_id[:8]}_water_pumped"
        start = datetime.now(timezone.utc) - timedelta(days=60)

        try:
            stats = await self._async_stats_during_period(start, {stat_id})
            return max(
                (ts for ts, mean in _stat_rows(stats, stat_id) if mean > 0),
                default=None,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not read last irrigation from statistics: %s", err)
            return None

    # ------------------------------------------------------------------
    # Forecast helpers
    # ------------------------------------------------------------------

    async def _async_build_forecasts(
        self, device_data: dict
    ) -> dict[str, PredictionResult]:
        """Return a PredictionResult for each device."""
        forecast_days = await self._async_get_weather_forecast()
        if not forecast_days:
            _LOGGER.debug("No weather forecast available; skipping prediction")
            return {}

        daily_temps = await self._async_get_daily_temps()

        results: dict[str, PredictionResult] = {}
        for device_id, data in device_data.items():
            level = current_level(
                data, self.tank_type(device_id), self.device_model(device_id)
            )
            if level is None:
                continue
            try:
                daily_consumption = await self._async_get_daily_consumption(device_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Consumption history unavailable for %s: %s", device_id[:8], err
                )
                daily_consumption = {}

            training_pairs: list[tuple[float, float]] = [
                (daily_temps[day], daily_consumption[day])
                for day in daily_consumption
                if day in daily_temps
            ]
            results[device_id] = compute_prediction(
                level, forecast_days, training_pairs
            )
            _LOGGER.debug(
                "Forecast for %s: %s days until empty (trained on %d days)",
                device_id[:8],
                results[device_id].days_until_empty,
                len(training_pairs),
            )

        return results

    async def _async_get_weather_forecast(self) -> list[DayForecast]:
        """Fetch 7-day daily forecast from the configured weather entity."""
        forecast_list: list[dict] = []
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": WEATHER_ENTITY, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            forecast_list = response.get(WEATHER_ENTITY, {}).get("forecast", [])
        except Exception:
            state = self.hass.states.get(WEATHER_ENTITY)
            if state:
                forecast_list = state.attributes.get("forecast", [])

        days: list[DayForecast] = []
        for f in forecast_list[:7]:
            try:
                dt = datetime.fromisoformat(str(f["datetime"])).date()
            except (KeyError, TypeError, ValueError):
                continue
            days.append(
                DayForecast(
                    date=dt,
                    temp_high=_coerce_float(
                        f.get("temperature"), f.get("native_temperature"), default=20.0
                    ),
                    temp_low=_coerce_float(
                        f.get("templow"), f.get("native_templow"), default=10.0
                    ),
                    precipitation_mm=_coerce_float(f.get("precipitation"), default=0.0),
                )
            )
        return days

    async def _async_get_daily_consumption(self, device_id: str) -> dict[date, float]:
        """Return daily water usage (L) from water_usage HA statistics (tank level drops)."""
        stat_id = f"{DOMAIN}:{device_id[:8]}_water_usage"
        start = datetime.now(timezone.utc) - timedelta(days=30)

        stats = await self._async_stats_during_period(start, {stat_id})
        daily: defaultdict[date, float] = defaultdict(float)
        for ts, mean in _stat_rows(stats, stat_id):
            if mean > 0:
                daily[ts.date()] += mean
        return dict(daily)

    async def _async_get_daily_temps(self) -> dict[date, float]:
        """Return daily average temperatures from weather entity state history."""
        start = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            history = await get_instance(self.hass).async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                None,
                [WEATHER_ENTITY],
            )
        except Exception as err:
            _LOGGER.debug("Weather state history unavailable: %s", err)
            return {}

        daily: defaultdict[date, list[float]] = defaultdict(list)
        for state in history.get(WEATHER_ENTITY, []):
            temp = state.attributes.get("temperature")
            if temp is None:
                continue
            try:
                daily[state.last_changed.date()].append(float(temp))
            except (TypeError, ValueError):
                continue
        return {day: sum(temps) / len(temps) for day, temps in daily.items()}
