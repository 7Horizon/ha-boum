"""Constants for the Boum integration."""
from datetime import timedelta

DOMAIN = "boum"
UPDATE_INTERVAL = timedelta(minutes=15)
API_BASE_URL = "https://api.boum.us/v1"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STATISTICS_HOURS = 168  # 7 days — hourly aggregates for HA statistics import
MINUTELY_HOURS = 96    # 4 days — per-minute data for sensors and consumption
WEATHER_ENTITY = "weather.openweathermap"

# Config / options keys
CONF_TANK_TYPE = "tank_type"
CONF_DEVICE_MODEL = "device_model"

# Valid values
TANK_TYPES = ["35l", "55l", "32l"]
DEVICE_MODELS = ["boum_2", "boum_3", "boum_core"]

DEFAULT_TANK_TYPE = "35l"
DEFAULT_DEVICE_MODEL = "boum_3"
