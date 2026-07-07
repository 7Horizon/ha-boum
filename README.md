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

The setup runs in three steps:

**Step 1 — Credentials**
Enter your Boum account e-mail address and password.

**Step 2 — Select Device**
Pick the device to add from a dropdown of the devices claimed in your account. Devices with a rejected claim status are not listed.

**Step 3 — Tank & Controller**
Select the tank size and controller model for the chosen device. These are needed to convert the ultrasonic sensor reading into an accurate water volume.

| Tank | Compatible controllers |
|---|---|
| 35 Liter | Boum 2, Boum 3 |
| 55 Liter | Boum 2, Boum 3 |
| 32 Liter | Boum Core |

After each device you can either add another one or finish the setup. Only devices configured here are polled and get entities.

More devices can be added later — and tank/controller of existing ones changed — via **Settings → Devices & Services → Boum → Configure**: pick the device from the dropdown, then adjust its settings.

---

## Sensors

### Water Level
**Unit:** L

The current volume of water in the tank in litres. Calculated by converting the raw ultrasonic sensor reading (`waterTableRange`, air gap in cm from sensor to water surface) using the tank-specific formula:

- **32L (Boum Core):** piecewise linear interpolation over an empirically measured lookup table from the Boum app
- **35L / 55L:** analytic frustum (truncated cone) formula — `V = (π/3) · h · (r₁² + r₁r₂ + r₂²)` — using the geometric dimensions of the respective tank model

Data source: per-minute API data (`interval=60s`), last 2 hours.

---

### Water Usage
**Unit:** L

Water volume that left the tank over the last 24 complete hours, calculated from consecutive tank level drops. This includes evaporation, leakage, and any passive seepage — not just what the pump delivered. This is the primary consumption metric used for Days Remaining and the weather-based forecast. Only completed hours enter the calculation and the window is aligned to hour boundaries, so the value changes at most once per hour.

Two outlier filters prevent the ultrasonic sensor from recording false drops when the controller lid is opened:

- **IQR filter (import)** — applied per-minute before the hourly mean is written to `water_level`. Statistical outlier readings within the same hour are excluded from the average.
- **Spike-and-recovery filter (computation)** — applied before writing `water_usage`. A level reading is classified as a spike and replaced with the average of its neighbours when the level drops by ≥ 1 L *and* recovers by ≥ 50 % of that drop in the very next hour. Real consumption never recovers, so genuine usage is not filtered.

Data source: HA long-term statistics (`boum:<id>_water_usage`).

---

### Water Pumped
**Unit:** L

Total water delivered by the irrigation pump in the last 24 complete hours. Derived from `pumpStopped` events in the device log (`GET /devices/{id}/log`), which report the exact volume measured per pump cycle (`payload.totalPumpedVolume`). Multiple cycles within the same hour are summed. The window is aligned to hour boundaries and the current (still-running) hour is excluded, so the value changes at most once per hour.

Data source: HA long-term statistics (`boum:<id>_water_pumped`).

---

### Last Irrigation
**Unit:** timestamp

The exact timestamp of the most recent pump cycle, taken from the `pumpStopped` event in the device log. This gives second-level precision.

If the device log window does not cover the most recent pump cycle (the log typically retains ~24 h of events), the sensor falls back to HA long-term statistics (`boum:<id>_water_pumped`), which have hourly precision and cover up to 60 days.

Data source: device log (primary, second precision) / HA long-term statistics (fallback, hourly precision).

---

### Days Remaining
**Unit:** days

Estimated days until the tank is empty, based on the average daily Water Usage (tank level drops) of the **last 3 complete days**.

```
days_remaining = current_level / avg_daily_water_usage
```

All days in the 3-day window are included in the average — including days with zero consumption — so that non-irrigation days correctly reduce the average and give a more accurate estimate. Today and the current hour are excluded from the calculation.

Extra attributes:
- `avg_daily_water_usage_liters` — the daily average consumption used in the calculation
- `days_in_window` — number of days in the averaging window (denominator)
- `days_with_consumption` — how many of those days had actual consumption

Data source: HA long-term statistics (`boum:<id>_water_usage`).

---

### Days Remaining (Forecast)
**Unit:** days

Weather-aware prediction of how many days the tank will last. Combines historical pump data with a 7-day weather forecast from `weather.openweathermap`. (Experimental)

**How it works:**

1. **Training data** — Daily water usage totals (from `boum:<id>_water_usage` in HA long-term statistics) and daily average temperatures (from weather entity state history) for the last 30 days are paired to form training examples.

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

The integration writes the following external statistics into the HA recorder (hourly resolution, updated every 15 minutes). Telemetry stats are fetched incrementally and grow over time; `water_pumped` and `water_usage` are derived from the device log and water level history respectively.

| Statistic ID | Unit | Description |
|---|---|---|
| `boum:<id>_water_usage` | L | Tank level drop per hour (spike-filtered) — feeds Water Usage sensor & predictions |
| `boum:<id>_water_pumped` | L | Water delivered by the pump per hour (from pumpStopped log events) — feeds Water Pumped sensor |
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

The coordinator polls the Boum API every **15 minutes**. Five requests are made per poll (one device list + four per device):

- **Device state** — current online/offline status
- **Per-minute data** (`interval=60s`, last 2 hours) — current sensor values (water level, temperature, battery, etc.)
- **Hourly data** (`interval=3600s`, incremental) — only data since the last known statistic is fetched and written to HA long-term statistics. On first install this backfills 7 days of history; on subsequent polls it typically covers 1–2 hours.
- **Device log** (`GET /devices/{id}/log`) — recent device events; `pumpStopped` entries are used to compute the `water_pumped` statistic.

Daily Water Usage, Days Remaining, and the forecast are calculated from HA long-term statistics. Last Irrigation is resolved from the device log first, falling back to statistics for older history.

---

## Notes

- The Boum API does not expose the device model (Boum 2 / Boum 3 / Boum Core) or tank size. You must configure these manually during setup; otherwise the water level calculation will be inaccurate.
- Flow rate is not tracked. Pump volume is measured precisely by the device firmware and reported as a `pumpStopped` log event after each cycle; this is more accurate than a flow-rate estimate and requires no separate sensor.
- Lifting the controller lid during operation can cause the ultrasonic sensor to report an unrealistically low water level. The integration filters these artefacts on two levels (see Daily Water Usage above) so they do not affect consumption or forecast calculations.
- This is an unofficial integration and is not affiliated with or endorsed by Boum.
