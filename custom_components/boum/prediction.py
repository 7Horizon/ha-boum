"""Water-level prediction — pure computation, no HA dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class DayForecast:
    date: date
    temp_high: float
    temp_low: float
    precipitation_mm: float


@dataclass
class PredictionResult:
    days_until_empty: int | None   # None = sufficient for entire forecast period
    predicted_empty_date: date | None
    daily_liters: list[float]      # predicted consumption per forecast day
    training_days: int             # historical days used to fit the model
    forecast_days: int             # length of the forecast window


def _fit_model(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares linear fit: consumption = a * avg_temp + b.

    Returns (a, b) or None if fewer than 3 data points.
    """
    n = len(pairs)
    if n < 3:
        return None

    sx = sum(t for t, _ in pairs)
    sy = sum(c for _, c in pairs)
    sxy = sum(t * c for t, c in pairs)
    sx2 = sum(t * t for t, _ in pairs)

    denom = n * sx2 - sx ** 2
    if denom == 0:
        return None

    a = max(0.0, (n * sxy - sx * sy) / denom)
    b = max(0.0, (sy - a * sx) / n)
    return a, b


def _predict_day(
    model: tuple[float, float] | None,
    day: DayForecast,
) -> float:
    """Predict water consumption in litres for one day."""
    t_avg = (day.temp_high + day.temp_low) / 2.0

    if model:
        a, b = model
        liters = a * t_avg + b
    else:
        # Physics-based prior: evapotranspiration grows above ~15 °C
        liters = max(0.0, 0.12 * (t_avg - 15.0))

    # Rainfall reduces irrigation need
    p = day.precipitation_mm
    if p >= 10:
        liters *= 0.2
    elif p >= 5:
        liters *= 0.5
    elif p >= 2:
        liters *= 0.8

    return round(max(0.0, liters), 2)


def compute_prediction(
    current_level: float,
    forecast: list[DayForecast],
    training_pairs: list[tuple[float, float]],
) -> PredictionResult:
    """Compute how many days the current tank level will last."""
    model = _fit_model(training_pairs)
    level = current_level
    daily_liters: list[float] = []

    for i, day in enumerate(forecast):
        consumption = _predict_day(model, day)
        daily_liters.append(consumption)
        level -= consumption
        if level <= 0:
            return PredictionResult(
                days_until_empty=i + 1,
                predicted_empty_date=day.date,
                daily_liters=daily_liters,
                training_days=len(training_pairs),
                forecast_days=len(forecast),
            )

    # Tank outlasts the forecast window — extrapolate with the average predicted
    # daily consumption from the forecast period.
    avg_daily = sum(daily_liters) / len(daily_liters) if daily_liters else 0.0
    if avg_daily > 0:
        days_until_empty = round(len(forecast) + level / avg_daily)
    else:
        days_until_empty = None  # zero predicted consumption, tank lasts indefinitely

    return PredictionResult(
        days_until_empty=days_until_empty,
        predicted_empty_date=None,
        daily_liters=daily_liters,
        training_days=len(training_pairs),
        forecast_days=len(forecast),
    )
