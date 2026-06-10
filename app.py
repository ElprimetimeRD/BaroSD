"""BaroSD — red barométrica para detección de microrráfagas en Santo Domingo.

UN SOLO ARCHIVO (patrón micron_bot): config + detección + API + agente semanal.
Deploy: Render Web Service · SQLite en disco persistente · alertas Telegram.

Endpoints:
  POST /readings/{device_id}   lecturas (header X-Api-Key) → respuesta incluye mode
  GET  /config                 modo actual (normal | storm)
  GET  /                       estado de la red
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ════════════════════════ CONFIG ════════════════════════


# --- Seguridad de la API (Render la genera sola con generateValue) ---
API_KEY = os.environ.get("API_KEY", "dev-key")

# --- Almacenamiento (disco persistente de Render) ---
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "barosd.db")

# --- Telegram ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Claude (solo agente semanal) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Umbrales de detección (v0.1, basados en literatura de mesohighs) ---
DETECTION = {
    "rise_hpa": 1.2,            # salto mínimo (mesohigh) en hPa
    "rise_window_min": 10,
    "v_drop_hpa": 0.5,          # couplet mesolow->mesohigh
    "v_rise_hpa": 1.0,
    "v_window_min": 15,
    "qc_max_std_hpa": 0.8,      # varianza alta = movimiento vertical, descartar
    "qc_max_gps_age_min": 30,
    "cluster_radius_km": 3.0,
    "cluster_window_min": 5,
    "min_devices_aviso": 3,
}


# --- Zona de interés: Gran Santo Domingo ---
SD_BOUNDS = {
    "lat_min": 18.38, "lat_max": 18.58,
    "lon_min": -70.05, "lon_max": -69.75,
}

METAR_STATION = "MDSD"  # Las Américas, referencia para bias (fase 2)


STORM_MINUTES = 60
READINGS_RETENTION_H = 48
ALERT_COOLDOWN_S = 15 * 60

# ════════════════════════ DETECCIÓN ════════════════════════





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


# ════════════════════════ AGENTE SEMANAL ════════════════════════






def _stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    week = time.time() - 7 * 86400
    by_level = conn.execute(
        "SELECT level, COUNT(*), AVG(magnitude), AVG(n_devices) FROM events "
        "WHERE ts>? GROUP BY level", (week,)).fetchall()
    n_dev = conn.execute(
        "SELECT COUNT(DISTINCT device_id) FROM readings WHERE ts>?",
        (time.time() - 86400,)).fetchone()[0]
    n_read = conn.execute(
        "SELECT COUNT(*) FROM readings WHERE ts>?",
        (time.time() - 86400,)).fetchone()[0]
    conn.close()
    return {
        "dispositivos_activos_24h": n_dev,
        "lecturas_24h": n_read,
        "eventos_7d": [
            {"level": r[0], "count": r[1],
             "avg_magnitude_hpa": round(r[2] or 0, 2),
             "avg_devices": round(r[3] or 0, 1)} for r in by_level],
    }


def _metar() -> dict:
    try:
        r = httpx.get("https://aviationweather.gov/api/data/metar",
                      params={"ids": METAR_STATION, "format": "json",
                              "hours": 24}, timeout=20)
        alts = [d.get("altim") for d in (r.json() or []) if d.get("altim")]
        return {"station": METAR_STATION, "n_obs_24h": len(alts),
                "rango_hpa": [min(alts), max(alts)] if alts else None}
    except Exception as e:
        return {"station": METAR_STATION, "error": str(e)}


def _agent_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN:
        print(text)
        return
    httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
               json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
               timeout=15)


def run_weekly_review() -> None:
    stats = _stats()
    metar = _metar()
    if not ANTHROPIC_API_KEY:
        _agent_telegram("📊 BaroSD semana (sin agente IA):\n"
                  + json.dumps(stats, indent=2, ensure_ascii=False))
        return

    prompt = f"""Eres el agente de calidad de BaroSD, red barométrica crowdsourced
para detectar microrráfagas en Santo Domingo.

Config actual:
{json.dumps(DETECTION, indent=2)}

Semana:
{json.dumps(stats, indent=2, ensure_ascii=False)}

Referencia METAR {metar.get('station')}:
{json.dumps(metar, indent=2, ensure_ascii=False)}

Tareas:
1. Diagnóstico breve (¿muchos eventos = falsos positivos? ¿cero = umbrales
   altos o red muy chica?).
2. Si corresponde, NUEVOS umbrales en un bloque JSON con solo las claves a cambiar.
3. Una recomendación accionable (máx 2 líneas).
Español, máx 150 palabras + JSON."""

    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5", "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        review = "".join(b.get("text", "") for b in r.json()["content"]
                         if b.get("type") == "text")
        _agent_telegram(f"🤖 BaroSD — revisión semanal:\n\n{review}")
    except Exception as e:
        _agent_telegram(f"⚠️ Agente semanal falló: {e}\nStats: {stats}")


# ════════════════════════ SERVER ════════════════════════





app = FastAPI(title="BaroSD", docs_url=None, redoc_url=None)
_last_alert: dict[str, float] = {}


# ---------- almacenamiento ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS readings (
        device_id TEXT, ts REAL, p REAL, lat REAL, lon REAL, gps_age REAL);
    CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
    CREATE TABLE IF NOT EXISTS events (
        ts REAL, level TEXT, lat REAL, lon REAL,
        n_devices INTEGER, magnitude REAL, raw TEXT);
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    conn.commit()
    conn.close()


def get_meta(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    conn.commit()


def storm_active(conn: sqlite3.Connection) -> bool:
    until = float(get_meta(conn, "storm_until", "0") or 0)
    return time.time() < until


# ---------- API ----------

def _check_key(x_api_key: str | None) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(401, "api key inválida")


@app.post("/readings/{device_id}")
async def post_readings(device_id: str, request: Request,
                        x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    body = await request.json()
    items = body if isinstance(body, list) else [body]
    conn = db()
    n = 0
    for v in items:
        try:
            conn.execute(
                "INSERT INTO readings VALUES (?,?,?,?,?,?)",
                (device_id, float(v["ts"]), float(v["p"]),
                 float(v["lat"]), float(v["lon"]),
                 float(v.get("gps_age", 0))))
            n += 1
        except (KeyError, TypeError, ValueError):
            continue
    conn.commit()
    mode = "storm" if storm_active(conn) else "normal"
    conn.close()
    return {"ok": True, "saved": n, "mode": mode}


@app.get("/config")
async def get_config():
    conn = db()
    mode = "storm" if storm_active(conn) else "normal"
    until = get_meta(conn, "storm_until", "0")
    conn.close()
    return {"mode": mode, "until": float(until or 0)}


@app.get("/")
async def status():
    conn = db()
    day_ago = time.time() - 86400
    n_dev = conn.execute(
        "SELECT COUNT(DISTINCT device_id) FROM readings WHERE ts>?",
        (day_ago,)).fetchone()[0]
    n_read = conn.execute(
        "SELECT COUNT(*) FROM readings WHERE ts>?", (day_ago,)).fetchone()[0]
    last_ev = conn.execute(
        "SELECT ts, level, n_devices, magnitude FROM events "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    mode = "storm" if storm_active(conn) else "normal"
    conn.close()
    return {
        "red": "BaroSD Santo Domingo", "mode": mode,
        "dispositivos_24h": n_dev, "lecturas_24h": n_read,
        "ultimo_evento": dict(zip(
            ("ts", "level", "n_devices", "magnitude"), last_ev)) if last_ev else None,
    }


# ---------- detector (tarea de fondo) ----------

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[telegram-dry] {text}")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"[telegram] {e}")


def run_detection_once() -> None:
    conn = db()
    cutoff = time.time() - 20 * 60
    rows = conn.execute(
        "SELECT device_id, ts, p, lat, lon, gps_age FROM readings "
        "WHERE ts>? ORDER BY ts", (cutoff,)).fetchall()

    by_device: dict[str, list[Reading]] = {}
    for r in rows:
        by_device.setdefault(r[0], []).append(Reading(*r))

    flags = [f for s in by_device.values() if (f := analyze_device(s))]
    if flags:
        for ev in cluster_flags(flags):
            # Cualquier señal escala TODA la red a modo tormenta
            set_meta(conn, "storm_until", str(time.time() + STORM_MINUTES * 60))

            key = f"{round(ev.center_lat, 2)}:{round(ev.center_lon, 2)}"
            if time.time() - _last_alert.get(key, 0) < ALERT_COOLDOWN_S:
                continue
            _last_alert[key] = time.time()

            emoji = "🔴" if ev.level == "AVISO" else "🟡"
            maps = f"https://maps.google.com/?q={ev.center_lat},{ev.center_lon}"
            send_telegram(
                f"{emoji} <b>BaroSD {ev.level}</b>\n"
                f"Posible firma de microrráfaga\n"
                f"Dispositivos coherentes: {ev.n_devices}\n"
                f"Salto medio: +{ev.mean_magnitude_hpa} hPa\n"
                f"Epicentro: <a href='{maps}'>{ev.center_lat}, {ev.center_lon}</a>\n"
                f"Red en modo tormenta por {STORM_MINUTES} min")
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                (ev.ts, ev.level, ev.center_lat, ev.center_lon,
                 ev.n_devices, ev.mean_magnitude_hpa,
                 json.dumps([f.__dict__ for f in ev.flags])))
            conn.commit()

    # limpieza de historial crudo
    conn.execute("DELETE FROM readings WHERE ts<?",
                 (time.time() - READINGS_RETENTION_H * 3600,))
    conn.commit()
    conn.close()


async def detector_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_detection_once)
        except Exception as e:
            print(f"[detector] {e}")
        await asyncio.sleep(60)


async def weekly_agent_loop() -> None:
    """Domingo ≥ 18:00 AST (22:00 UTC), una vez por semana."""
    while True:
        try:
            now = time.gmtime()
            week_id = f"{now.tm_year}-{now.tm_yday // 7}"
            conn = db()
            done = get_meta(conn, "agent_week", "")
            if now.tm_wday == 6 and now.tm_hour >= 22 and done != week_id:
                await asyncio.to_thread(run_weekly_review)
                set_meta(conn, "agent_week", week_id)
            conn.close()
        except Exception as e:
            print(f"[agente] {e}")
        await asyncio.sleep(1800)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    asyncio.create_task(detector_loop())
    asyncio.create_task(weekly_agent_loop())
    send_telegram("🌀 BaroSD server iniciado")
