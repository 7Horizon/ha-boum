"""Boum sensor platform."""
from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, timedelta, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BoumCoordinator
from .tank import water_level_liters

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, kw_only=True)
class BoumSensorEntityDescription(SensorEntityDescription):
    """SensorEntityDescription extended with the Boum telemetry key."""

    telemetry_key: str = ""


# Telemetry sensors that map 1-to-1 to API fields (no unit conversion needed).
# Flow rate is intentionally not exposed as a sensor: with 15-min polling the
# pump is always off at poll time. Pump volume data lives in HA statistics
# (boum:xxxx_water_pumped), computed from pumpStopped log events.
SENSOR_DESCRIPTIONS: tuple[BoumSensorEntityDescription, ...] = (
    BoumSensorEntityDescription(
        key="temperature",
        telemetry_key="temperature",
        translation_key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BoumSensorEntityDescription(
        key="temperature_esp",
        telemetry_key="temperatureEsp",
        translation_key="temperature_esp",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BoumSensorEntityDescription(
        key="battery_capacity",
        telemetry_key="batteryCapacity",
        translation_key="battery_capacity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BoumSensorEntityDescription(
        key="battery_voltage",
        telemetry_key="batteryVoltage",
        translation_key="battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BoumSensorEntityDescription(
        key="battery_current",
        telemetry_key="batteryCurrent",
        translation_key="battery_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BoumSensorEntityDescription(
        key="solar_voltage",
        telemetry_key="solarVoltage",
        translation_key="solar_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BoumSensorEntityDescription(
        key="input_current",
        telemetry_key="inputCurrent",
        translation_key="input_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BoumSensorEntityDescription(
        key="wifi_strength",
        telemetry_key="wifiStrength",
        translation_key="wifi_strength",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Boum sensor entities from a config entry."""
    coordinator: BoumCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for device_id in coordinator.data:
        entities.append(BoumWaterLevelSensor(coordinator, device_id))
        entities.extend(
            BoumSensor(coordinator, device_id, desc) for desc in SENSOR_DESCRIPTIONS
        )
        entities.append(BoumLastIrrigationSensor(coordinator, device_id))
        entities.append(BoumWaterUsageSensor(coordinator, device_id))
        entities.append(BoumWaterPumpedSensor(coordinator, device_id))
        entities.append(BoumDaysRemainingSensor(coordinator, device_id))
        entities.append(BoumWaterForecastSensor(coordinator, device_id))
    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_info(device_id: str, device_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=device_name,
        manufacturer="Boum",
        model="Smart Irrigation Controller",
    )


def _device_name(coordinator: BoumCoordinator, device_id: str) -> str:
    return coordinator.data.get(device_id, {}).get("name", f"Boum {device_id[:8]}")


def _latest_value(time_series: dict, key: str) -> float | None:
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


def _current_level(
    device_data: dict, tank_type: str, device_model: str
) -> float | None:
    """Return current water level in litres from coordinator device data."""
    for data_key in ("minutely", "hourly"):
        raw = _latest_value(
            device_data.get(data_key, {}).get("timeSeries", {}), "waterTableRange"
        )
        if raw is not None:
            return water_level_liters(raw, tank_type, device_model)
    return None


# ---------------------------------------------------------------------------
# Sensor classes
# ---------------------------------------------------------------------------

class BoumWaterLevelSensor(CoordinatorEntity, SensorEntity):
    """Water level in litres using the tank-specific formula from config."""

    _attr_has_entity_name = True
    _attr_translation_key = "water_level"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water"

    def __init__(self, coordinator: BoumCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_water_level"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    @property
    def native_value(self) -> float | None:
        level = _current_level(
            self.coordinator.data.get(self._device_id, {}),
            self.coordinator.tank_type,
            self.coordinator.device_model,
        )
        return round(level, 2) if level is not None else None


class BoumSensor(CoordinatorEntity, SensorEntity):
    """Generic Boum telemetry sensor (direct API value, no conversion)."""

    _attr_has_entity_name = True
    entity_description: BoumSensorEntityDescription

    def __init__(
        self,
        coordinator: BoumCoordinator,
        device_id: str,
        description: BoumSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_{description.key}"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    @property
    def native_value(self) -> float | None:
        device_data = self.coordinator.data.get(self._device_id, {})
        for data_key in ("minutely", "hourly"):
            time_series = device_data.get(data_key, {}).get("timeSeries", {})
            raw = _latest_value(time_series, self.entity_description.telemetry_key)
            if raw is not None:
                return round(raw, 2)
        return None


class BoumLastIrrigationSensor(CoordinatorEntity, SensorEntity):
    """Timestamp of the last irrigation event from the most recent pumpStopped log entry."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_irrigation"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(self, coordinator: BoumCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_last_irrigation"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.get(self._device_id, {}).get("last_irrigation")


class BoumWaterUsageSensor(CoordinatorEntity, SensorEntity):
    """Water consumed from the tank over the last 24 hours, from HA statistics.

    Derived from tank level drops (boum:<id>_water_usage statistic), spike-filtered.
    Includes evaporation and leakage — reflects actual tank depletion.
    Also the basis for Days Remaining and the weather forecast.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "water_usage"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-circle"

    def __init__(self, coordinator: BoumCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_water_usage"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    @property
    def native_value(self) -> float | None:
        water_usage = (
            self.coordinator.data
            .get(self._device_id, {})
            .get("hass_stats", {})
            .get("water_usage", {})
        )
        if not water_usage:
            return None

        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        cutoff = now - timedelta(hours=24)

        values = [val for ts, val in water_usage.items() if cutoff <= ts < current_hour]
        return round(sum(values), 1) if values else None


class BoumWaterPumpedSensor(CoordinatorEntity, SensorEntity):
    """Water delivered by the pump in the last 24 hours, from HA statistics.

    Derived from pumpStopped log events (payload.totalPumpedVolume) aggregated
    into the boum:<id>_water_pumped statistic.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "water_pumped"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:pump"

    def __init__(self, coordinator: BoumCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_water_pumped"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    @property
    def native_value(self) -> float | None:
        water_pumped = (
            self.coordinator.data
            .get(self._device_id, {})
            .get("hass_stats", {})
            .get("water_pumped", {})
        )
        if not water_pumped:
            return None

        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        cutoff = now - timedelta(hours=24)

        values = [val for ts, val in water_pumped.items() if cutoff <= ts < current_hour]
        return round(sum(values), 1) if values else None


class BoumDaysRemainingSensor(CoordinatorEntity, SensorEntity):
    """Days until empty based on the 3-day average daily water pumped."""

    _attr_has_entity_name = True
    _attr_translation_key = "days_remaining"
    _attr_native_unit_of_measurement = "d"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:calendar"

    def __init__(self, coordinator: BoumCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_days_remaining"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    def _daily_totals(self, device_data: dict) -> dict[date, float]:
        """Sum hourly water pumped per day over the last 3 complete days, from HA statistics.

        All complete days in the window are pre-seeded with 0.0 so that days
        with no irrigation are counted in the average denominator.
        """
        water_usage = device_data.get("hass_stats", {}).get("water_usage", {})
        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        today_utc = now.date()
        cutoff = now - timedelta(days=3)

        yesterday = today_utc - timedelta(days=1)
        window_start = cutoff.date() + timedelta(days=1)
        totals: dict[date, float] = {}
        d = window_start
        while d <= yesterday:
            totals[d] = 0.0
            d += timedelta(days=1)

        for ts, val in water_usage.items():
            if ts < cutoff or ts >= current_hour or ts.date() >= today_utc:
                continue
            if ts.date() in totals:
                totals[ts.date()] += val
        return totals

    @property
    def native_value(self) -> float | None:
        device_data = self.coordinator.data.get(self._device_id, {})
        level = _current_level(
            device_data, self.coordinator.tank_type, self.coordinator.device_model
        )
        if level is None:
            return None
        daily = self._daily_totals(device_data)
        if not daily:
            return None
        avg = sum(daily.values()) / len(daily)
        return round(level / avg, 1) if avg > 0 else None

    @property
    def extra_state_attributes(self) -> dict:
        device_data = self.coordinator.data.get(self._device_id, {})
        daily = self._daily_totals(device_data)
        avg = sum(daily.values()) / len(daily) if daily else None
        return {
            "avg_daily_water_usage_liters": round(avg, 2) if avg is not None else None,
            "days_in_window": len(daily),
            "days_with_consumption": sum(1 for v in daily.values() if v > 0),
        }


class BoumWaterForecastSensor(CoordinatorEntity, SensorEntity):
    """Days until empty based on weather forecast and historical consumption."""

    _attr_has_entity_name = True
    _attr_translation_key = "days_until_empty"
    _attr_native_unit_of_measurement = "d"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar"

    def __init__(self, coordinator: BoumCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_{device_id}_days_until_empty"
        self._attr_device_info = _device_info(device_id, _device_name(coordinator, device_id))

    @property
    def native_value(self) -> int | None:
        forecast = self.coordinator.data.get(self._device_id, {}).get("forecast")
        return forecast.days_until_empty if forecast is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        forecast = self.coordinator.data.get(self._device_id, {}).get("forecast")
        if forecast is None:
            return {}
        within_forecast = forecast.predicted_empty_date is not None
        return {
            "within_forecast_period": within_forecast,
            "extrapolated": not within_forecast and forecast.days_until_empty is not None,
            "forecast_horizon_days": forecast.forecast_days,
            "predicted_empty_date": (
                forecast.predicted_empty_date.isoformat()
                if forecast.predicted_empty_date
                else None
            ),
            "daily_predictions_liters": forecast.daily_liters,
            "training_days": forecast.training_days,
        }
