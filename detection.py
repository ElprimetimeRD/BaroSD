"""Detección de firmas de microrráfaga sobre series de presión crowdsourced.

Lógica en dos niveles:
1. Por dispositivo: detectar salto de presión (mesohigh) o couplet en V
   sobre la TENDENCIA (el bias absoluto del sensor se cancela).
2. Espacial: agrupar flags coherentes en tiempo y espacio para separar
   un evento meteorológico real de ruido individual (bolsillo, ascensor).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .config import DETECTION


@dataclass
class Reading:
    device_id: str
    ts: float          # epoch segundos
    pressure: float    # hPa
    lat: float
    lon: float
    gps_age_min: float = 0.0


@dataclass
class DeviceFlag:
    device_id: str
    ts: float
    lat: float
    lon: float
    kind: str          # "rise" | "v_shape"
    magnitude_hpa: float


@dataclass
class Event:
    level: str                     # "VIGILANCIA" | "AVISO"
    ts: float
    center_lat: float
    center_lon: float
    n_devices: int
    mean_magnitude_hpa: float
    flags: list[DeviceFlag] = field(default_factory=list)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def analyze_device(readings: list[Reading]) -> DeviceFlag | None:
    """Analiza la serie reciente de UN dispositivo. Devuelve flag o None.

    `readings` debe venir ordenado por ts ascendente y cubrir ~15 min.
    """
    cfg = DETECTION
    if len(readings) < 4:
        return None

    last = readings[-1]

    # QC: ubicación confiable
    if last.gps_age_min > cfg["qc_max_gps_age_min"]:
        return None

    # QC: varianza de los últimos 5 min (movimiento vertical contamina)
    cutoff_qc = last.ts - 5 * 60
    recent = [r.pressure for r in readings if r.ts >= cutoff_qc]
    if _std(recent) > cfg["qc_max_std_hpa"]:
        return None

    # 1) Salto tipo mesohigh: min de la ventana -> valor actual
    cutoff_rise = last.ts - cfg["rise_window_min"] * 60
    window = [r for r in readings if r.ts >= cutoff_rise]
    if window:
        p_min = min(r.pressure for r in window)
        rise = last.pressure - p_min
        if rise >= cfg["rise_hpa"]:
            return DeviceFlag(last.device_id, last.ts, last.lat, last.lon,
                              "rise", round(rise, 2))

    # 2) Couplet en V: caída y luego subida dentro de la ventana V
    cutoff_v = last.ts - cfg["v_window_min"] * 60
    vwin = [r for r in readings if r.ts >= cutoff_v]
    if len(vwin) >= 4:
        p0 = vwin[0].pressure
        i_min = min(range(len(vwin)), key=lambda i: vwin[i].pressure)
        p_min = vwin[i_min].pressure
        drop = p0 - p_min
        recovery = last.pressure - p_min
        if (drop >= cfg["v_drop_hpa"] and recovery >= cfg["v_rise_hpa"]
                and i_min < len(vwin) - 1):
            return DeviceFlag(last.device_id, last.ts, last.lat, last.lon,
                              "v_shape", round(recovery, 2))

    return None


def cluster_flags(flags: list[DeviceFlag]) -> list[Event]:
    """Agrupa flags coherentes en espacio/tiempo y los eleva a eventos."""
    cfg = DETECTION
    now = time.time()
    active = [f for f in flags if now - f.ts <= cfg["cluster_window_min"] * 60]
    events: list[Event] = []
    used: set[str] = set()

    for f in active:
        if f.device_id in used:
            continue
        group = [g for g in active
                 if g.device_id not in used
                 and _haversine_km(f.lat, f.lon, g.lat, g.lon) <= cfg["cluster_radius_km"]]
        for g in group:
            used.add(g.device_id)

        n = len(group)
        if n == 0:
            continue
        w = [g.magnitude_hpa for g in group]
        total = sum(w) or 1.0
        c_lat = sum(g.lat * g.magnitude_hpa for g in group) / total
        c_lon = sum(g.lon * g.magnitude_hpa for g in group) / total
        level = "AVISO" if n >= cfg["min_devices_aviso"] else "VIGILANCIA"
        events.append(Event(
            level=level, ts=now,
            center_lat=round(c_lat, 5), center_lon=round(c_lon, 5),
            n_devices=n,
            mean_magnitude_hpa=round(sum(w) / n, 2),
            flags=group,
        ))
    return events
