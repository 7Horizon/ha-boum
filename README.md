# Boum — Home Assistant Integration

Unofficial Home Assistant integration for the [Boum](https://boum.garden) smart garden irrigation system. Many thanks to the people from Boum for providing the logic for calculating some of the values. Provides water level monitoring, consumption tracking, and an intelligent prediction of how many days the tank will last. The integration is still in an early stage, feel free to submit any suggestions. 

---

## Prerequisites

- **Home Assistant** 2024.11 or newer
- A **Boum account** with at least one claimed device
- For weather-based prediction: the **OpenWeatherMap** integration configured in HA (`weather.openweathermap`)

---

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Search for **Boum** and install
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/boum` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Setup

Go to **Settings → Devices & Services → Add Integration** and search for **Boum**.

The setup runs in two steps:

**Step 1 — Credentials**
Enter your Boum account e-mail address and password.

**Step 2 — Tank & Controller**
Select the tank size and controller model. These are needed to convert the ultrasonic sensor reading into an accurate water volume.

| Tank | Compatible controllers |
|---|---|
| 35 Liter | Boum 2, Boum 3 |
| 55 Liter | Boum 2, Boum 3 |
| 32 Liter | Boum Core |

Tank type and controller can be changed later via **Settings → Devices & Services → Boum → Configure**.

---

## Sensors

### Water Level
**Unit:** L

The current volume of water in the tank in litres. Calculated by converting the raw ultrasonic sensor reading (`waterTableRange`, air gap in cm from sensor to water surface) using the tank-specific formula:

- **32L (Boum Core):** piecewise linear interpolation over an empirically measured lookup table from the Boum app
- **35L / 55L:** analytic frustum (truncated cone) formula — `V = (π/3) · h · (r₁² + r₁r₂ + r₂²)` — using the geometric dimensions of the respective tank model

Data source: per-minute API data (`interval=60s`), last 2 hours.

---

### Daily Water Usage
**Unit:** L

Actual water consumed from the tank over the last 24 hours. Calculated from the hourly water level values stored in HA long-term statistics (`boum:<id>_water_level`) by summing all consecutive level drops. Level increases (refills, sensor noise) are ignored. This reflects the true water volume that left the tank, including evaporation and leakage, not just what the pump delivered.

Two outlier filters are applied to prevent the ultrasonic sensor from reporting false consumption when the controller lid is opened:

- **IQR filter (import)** — applied per-minute before the hourly mean is written to HA statistics. Per-minute readings that are statistical outliers (IQR method) within the same hour are excluded from the average.
- **Spike-and-recovery filter (calculation)** — applied to already-stored hourly statistics. A reading is classified as a spike and replaced with the average of its neighbours when the level drops by ≥ 3 L *and* recovers by ≥ 50 % of that drop in the very next hour.

Data source: HA long-term statistics.

---

### Water Pumped
**Unit:** L

Total water delivered by the irrigation pump in the last 24 hours. Derived from the flow rate recorded in HA long-term statistics (`boum:<id>_water_pumped`). The current (still-running) hour is excluded to avoid a fluctuating value.

Data source: HA long-term statistics.

---

### Last Irrigation
**Unit:** timestamp

The most recent hour in which a non-zero flow rate was recorded. Resolved from HA long-term statistics (`boum:<id>_flow_rate`), giving up to 60 days of history. Falls back to the per-minute API window on first install before statistics exist.

Data source: HA long-term statistics (hourly precision).

---

### Days Remaining
**Unit:** days

Estimated days until the tank is empty, based on the average daily water usage of the **last 3 complete days**.

```
days_remaining = current_level / avg_daily_water_usage
```

All days in the 3-day window are included in the average — including days with zero usage — so that non-irrigation days correctly reduce the average and give a more accurate estimate. Today and the current hour are excluded from the calculation.

Extra attributes:
- `avg_daily_water_usage_liters` — the daily average water usage used in the calculation
- `days_in_window` — number of days in the averaging window (denominator)
- `days_with_consumption` — how many of those days had actual water usage

Data source: HA long-term statistics.

---

### Days Remaining (Forecast)
**Unit:** days

Weather-aware prediction of how many days the tank will last. Combines historical consumption data with a 7-day weather forecast from `weather.openweathermap`. (Experimental)

**How it works:**

1. **Training data** — Daily water usage totals (from tank level drops in HA long-term statistics) and daily average temperatures (from weather entity state history) for the last 30 days are paired to form training examples.

2. **Model fitting** — A linear regression is fitted: `consumption = a × avg_temp + b`. Requires at least 3 days of paired data. If less data is available, a physics-based heuristic is used instead: `max(0, 0.12 × (avg_temp − 15))`, assuming evapotranspiration grows above 15 °C.

3. **Rain reduction** — Predicted consumption is reduced based on precipitation:
   - ≥ 10 mm → × 0.2
   - ≥ 5 mm → × 0.5
   - ≥ 2 mm → × 0.8

4. **Forward simulation** — The current tank level is decremented by the predicted daily consumption for each of the 7 forecast days. If the level reaches zero within the forecast window, the day count is returned.

5. **Extrapolation** — If the tank outlasts the 7-day forecast, the average predicted consumption from the forecast period is used to estimate the remaining days beyond the window: `7 + remaining_level / avg_predicted_daily`.

The prediction improves automatically over time as more paired consumption/temperature data accumulates in HA statistics.

Extra attributes:
- `within_forecast_period` — `true` if the tank runs out within the 7-day forecast window
- `extrapolated` — `true` if the value extends beyond the forecast window (less precise)
- `forecast_horizon_days` — number of forecast days used (typically 7)
- `predicted_empty_date` — ISO date when the tank is predicted to run dry (or `null`)
- `daily_predictions_liters` — predicted consumption per forecast day
- `training_days` — number of historical day-pairs used to train the model

Requires: `weather.openweathermap` integration.

---

## Diagnostic Sensors

The following sensors are available under the device but **disabled by default**. Enable them individually in HA if needed.

| Sensor | Unit | Description |
|---|---|---|
| ESP Temperature | °C | Internal controller board temperature |
| Battery Voltage | V | Battery voltage |
| Battery Current | A | Battery charge/discharge current |
| Solar Voltage | V | Solar panel input voltage |
| Input Current | A | Total input current |
| Wi-Fi Signal | dBm | Wi-Fi signal strength |

The **Battery** sensor (%) is enabled by default.

---

## HA Long-Term Statistics

The integration writes the following external statistics into the HA recorder (hourly resolution, 7-day rolling window, updated every 15 minutes):

| Statistic ID | Unit | Description |
|---|---|---|
| `boum:<id>_flow_rate` | L/min | Hourly average flow rate |
| `boum:<id>_water_pumped` | L/h | Water delivered by the pump per hour |
| `boum:<id>_water_level` | L | Tank water level |
| `boum:<id>_temperature` | °C | Environment temperature |
| `boum:<id>_temperature_esp` | °C | Controller temperature |
| `boum:<id>_battery_capacity` | % | Battery charge |
| `boum:<id>_battery_voltage` | V | Battery voltage |
| `boum:<id>_battery_current` | A | Battery current |
| `boum:<id>_solar_voltage` | V | Solar voltage |
| `boum:<id>_input_current` | A | Input current |
| `boum:<id>_wifi_strength` | dBm | Wi-Fi signal |

`<id>` is the first 8 characters of the device ID. These statistics are accessible via **Developer Tools → Statistics** or with an ApexCharts card using `statistic_id`.

---

## Data Refresh

The coordinator polls the Boum API every **15 minutes**. Four requests are made per poll (one device list + three per device):

- **Device state** — current online/offline status
- **Per-minute data** (`interval=60s`, last 2 hours) — current sensor values (water level, temperature, battery, etc.)
- **Hourly data** (`interval=3600s`, incremental) — only data since the last known statistic is fetched and written to HA long-term statistics. On first install this backfills 7 days of history; on subsequent polls it typically covers 1–2 hours.

Water usage, days remaining, and last irrigation are all calculated from HA long-term statistics rather than from the API, keeping API traffic minimal.

---

## Notes

- The Boum API does not expose the device model (Boum 2 / Boum 3 / Boum Core) or tank size. You must configure these manually during setup; otherwise the water level calculation will be inaccurate.
- The flow rate entity sensor is intentionally not included. With 15-minute polling intervals and typical pump runs lasting only a few minutes, the sensor would always read 0. Flow data is available via the HA long-term statistics described above.
- Lifting the controller lid during operation can cause the ultrasonic sensor to report an unrealistically low water level. The integration filters these artefacts on two levels (see Daily Water Usage above) so they do not affect consumption or forecast calculations.
- This is an unofficial integration and is not affiliated with or endorsed by Boum.
