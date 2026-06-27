"""Tank geometry and water-level calculations — ported from the Boum app."""
from __future__ import annotations

import math

# Boum Core 32L — empirically measured lookup table: (mm_from_sensor, litres_remaining)
_CORE_32L: tuple[tuple[float, float], ...] = (
    (50, 32.00), (60, 30.86), (70, 29.71), (80, 28.58), (90, 27.45),
    (100, 26.34), (110, 25.23), (120, 24.13), (130, 23.04), (140, 21.96),
    (150, 20.89), (160, 19.83), (170, 18.78), (180, 17.73), (190, 16.70),
    (200, 15.67), (210, 14.66), (220, 13.65), (230, 12.65), (240, 11.66),
    (250, 10.68), (260, 9.71), (270, 8.74), (280, 7.79), (290, 6.84),
    (300, 5.92), (310, 5.01), (320, 4.13), (330, 3.29), (340, 2.48),
    (350, 1.72), (360, 1.00), (370, 0.35), (380, 0.00),
)

# Geometric parameters for the 35L and 55L frustum tanks.
# HEIGHT is keyed by device_model (varies slightly by manufacturing batch).
# SLOPE is the half-angle of the cone wall in degrees.
# RADIUS_M is the inner radius at the tank bottom (metres).
_GEOMETRY: dict[str, dict] = {
    "35l": {
        "slope_deg": 6.5,
        "height_m": {"boum_2": 0.39, "boum_3": 0.41, "boum_core": 0.41},
        "radius_m": 0.14744,
        "capacity_l": 35.0,
    },
    "55l": {
        "slope_deg": 4.0,
        "height_m": {"boum_2": 0.6832, "boum_3": 0.6902, "boum_core": 0.6902},
        "radius_m": 0.1426,
        "capacity_l": 55.0,
    },
}


def water_level_liters(raw_cm: float, tank_type: str, device_model: str) -> float:
    """Convert waterTableRange (cm, sensor → water surface) to remaining litres.

    Dispatches to the lookup-table strategy for '32l' and the analytic frustum
    formula for '35l' / '55l', mirroring the Boum mobile app's WaterVolumeService.
    """
    if tank_type == "32l":
        return _core_32l(raw_cm)
    return _geometric(raw_cm, tank_type, device_model)


def tank_capacity_liters(tank_type: str) -> float:
    """Nominal tank capacity in litres for the given tank type."""
    if tank_type == "32l":
        return 32.0
    return _GEOMETRY.get(tank_type, _GEOMETRY["35l"])["capacity_l"]


def _core_32l(raw_cm: float) -> float:
    """Piecewise-linear interpolation over the Boum Core 32L empirical table."""
    mm = raw_cm * 10.0
    if mm <= _CORE_32L[0][0]:
        return 32.0
    if mm >= _CORE_32L[-1][0]:
        return 0.0
    for i in range(len(_CORE_32L) - 1):
        mm1, l1 = _CORE_32L[i]
        mm2, l2 = _CORE_32L[i + 1]
        if mm1 <= mm <= mm2:
            ratio = (mm - mm1) / (mm2 - mm1)
            return max(0.0, l1 + ratio * (l2 - l1))
    return 0.0


def _geometric(raw_cm: float, tank_type: str, device_model: str) -> float:
    """Analytic frustum (truncated cone) volume for 35L / 55L tanks.

    V = (π/3) · h · (r₁² + r₁·r₂ + r₂²)
    where h  = water column height (m),
          r₁ = radius at tank bottom (m),
          r₂ = radius at water surface (m, derived from cone slope).
    """
    geo = _GEOMETRY.get(tank_type, _GEOMETRY["35l"])
    h_tank = geo["height_m"].get(device_model, geo["height_m"]["boum_3"])
    slope_rad = math.radians(geo["slope_deg"])
    r_bottom = geo["radius_m"]

    h_water = h_tank - raw_cm / 100.0
    r_surface = h_water * math.tan(slope_rad) + r_bottom
    volume_m3 = (math.pi / 3.0) * h_water * (
        r_bottom ** 2 + r_bottom * r_surface + r_surface ** 2
    )
    return max(0.0, volume_m3 * 1000.0)
