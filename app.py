"""BaroSD server — TODO en un solo Web Service de Render.

- POST /readings/{device_id}  ingesta de lecturas (la respuesta incluye el
  modo actual, así el teléfono no necesita una segunda petición)
- GET  /config                modo actual (normal | storm)
- GET  /                      estado de la red
- Tarea de fondo 1: detector cada 60 s (flags -> clusters -> Telegram)
- Tarea de fondo 2: agente semanal (domingo 18:00 AST) con Claude API

SQLite en el disco persistente, mismo patrón que micron_bot.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from . import agent
from .config import (API_KEY, DB_PATH, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                     STORM_MINUTES, READINGS_RETENTION_H, ALERT_COOLDOWN_S)
from .detection import Reading, analyze_device, cluster_flags

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
                await asyncio.to_thread(agent.run_weekly_review)
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
