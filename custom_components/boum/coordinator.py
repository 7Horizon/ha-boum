"""DataUpdateCoordinator for the Boum integration."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BoumApi, BoumApiError
from .const import (
    CONF_DEVICE_MODEL,
    CONF_TANK_TYPE,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_TANK_TYPE,
    DOMAIN,
    MINUTELY_HOURS,
    STATISTICS_HOURS,
    UPDATE_INTERVAL,
    WEATHER_ENTITY,
)
from .prediction import DayForecast, PredictionResult, compute_prediction
from .statistics import import_statistics
from .tank import water_level_liters

_LOGGER = logging.getLogger(__name__)


def _latest_level(device_data: dict, tank_type: str, device_model: str) -> float | None:
    """Return current water level in litres from coordinator device data."""
    for data_key in ("minutely", "hourly"):
        for point in reversed(
            device_data.get(data_key, {}).get("timeSeries", {}).get("waterTableRange", [])
        ):
            v = point.get("y")
            if v is None:
                continue
            try:
                return water_level_liters(float(v), tank_type, device_model)
            except (TypeError, ValueError):
                continue
    return None


class BoumCoordinator(DataUpdateCoordinator[dict]):
    """Fetch data for all claimed Boum devices."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, api: BoumApi, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.api = api
        self.config_entry = entry

    @property
    def tank_type(self) -> str:
        return self.config_entry.options.get(CONF_TANK_TYPE, DEFAULT_TANK_TYPE)

    @property
    def device_model(self) -> str:
        return self.config_entry.options.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)

    async def _async_update_data(self) -> dict:
        try:
            devices = await self.api.get_claimed_devices()
        except BoumApiError as err:
            raise UpdateFailed(f"Could not fetch device list: {err}") from err

        if not devices:
            _LOGGER.warning("No claimed Boum devices found for this account")
            return {}

        now = datetime.now(timezone.utc)
        minutely_start = now - timedelta(hours=MINUTELY_HOURS)
        hourly_start = now - timedelta(hours=STATISTICS_HOURS)

        result: dict = {}
        for device in devices:
            device_id = device["id"]
            device_name = device["name"] or f"Boum {device_id[:8]}"
            try:
                state = await self.api.get_device_state(device_id)
                minutely = await self.api.get_device_telemetry(
                    device_id, minutely_start, now, interval="60s"
                )
                hourly = await self.api.get_device_telemetry(
                    device_id, hourly_start, now, interval="3600s"
                )
                _LOGGER.debug(
                    "Device %s: minutely keys=%s, hourly keys=%s",
                    device_id,
                    list(minutely.get("timeSeries", {}).keys()),
                    list(hourly.get("timeSeries", {}).keys()),
                )
                result[device_id] = {
                    "name": device_name,
                    "state": state,
                    "minutely": minutely,
                    "hourly": hourly,
                }
                try:
                    import_statistics(
                        self.hass, device_id, hourly, self.tank_type, self.device_model
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Statistics import failed for %s: %s", device_id, err)
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
            current_level = _latest_level(data, self.tank_type, self.device_model)
            if current_level is None:
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
                current_level, forecast_days, training_pairs
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
                days.append(
                    DayForecast(
                        date=dt,
                        temp_high=float(
                            f.get("temperature") or f.get("native_temperature") or 20
                        ),
                        temp_low=float(
                            f.get("templow") or f.get("native_templow") or 10
                        ),
                        precipitation_mm=float(f.get("precipitation") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return days

    async def _async_get_daily_consumption(self, device_id: str) -> dict[date, float]:
        """Return daily consumption totals (L) from HA recorder statistics."""
        short_id = device_id[:8]
        stat_id = f"{DOMAIN}:{short_id}_hourly_consumption"
        start = datetime.now(timezone.utc) - timedelta(days=30)

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            None,
            {stat_id},
            "hour",
            None,
            {"mean"},
        )

        daily: defaultdict[date, float] = defaultdict(float)
        for row in stats.get(stat_id, []):
            if row.mean is not None:
                daily[row.start.date()] += row.mean
        return dict(daily)

    async def _async_get_daily_temps(self) -> dict[date, float]:
        """Return daily average temperatures from weather entity state history."""
        start = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

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
