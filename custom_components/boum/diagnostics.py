"""Diagnostics for Boum — exposes raw coordinator data for debugging."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import BoumCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator: BoumCoordinator = hass.data[DOMAIN][entry.entry_id]

    data: dict = {}
    for device_id, device_data in coordinator.data.items():
        # Exclude the raw hourly/recent blobs to keep diagnostics readable.
        entry_data: dict = {
            "name": device_data.get("name"),
            "tank_type": coordinator.tank_type,
            "device_model": coordinator.device_model,
            "telemetry_keys_minutely": list(
                device_data.get("minutely", {}).get("timeSeries", {}).keys()
            ),
            "telemetry_keys_hourly": list(
                device_data.get("hourly", {}).get("timeSeries", {}).keys()
            ),
        }
        forecast = device_data.get("forecast")
        if forecast is not None:
            entry_data["forecast"] = {
                "sufficient_for_forecast_period": forecast.days_until_empty is None,
                "days_until_empty": forecast.days_until_empty,
                "forecast_horizon_days": forecast.forecast_days,
                "predicted_empty_date": (
                    forecast.predicted_empty_date.isoformat()
                    if forecast.predicted_empty_date
                    else None
                ),
                "training_days": forecast.training_days,
                "daily_liters": forecast.daily_liters,
            }
        data[device_id] = entry_data

    return {
        "options": dict(entry.options),
        "devices": data,
    }
