"""Agente de mejora continua (corre dentro del server, domingos 18:00 AST).

1. Estadísticas de la semana desde el SQLite local.
2. Presión de referencia METAR MDSD (aviationweather.gov).
3. Claude API: diagnóstico + propuesta de umbrales en JSON.
4. Resultado por Telegram. Tú decides si aplicar.
"""
from __future__ import annotations

import json
import sqlite3
import time

import httpx

from .config import (ANTHROPIC_API_KEY, DB_PATH, DETECTION, METAR_STATION,
                     TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)


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


def _telegram(text: str) -> None:
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
        _telegram("📊 BaroSD semana (sin agente IA):\n"
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
        _telegram(f"🤖 BaroSD — revisión semanal:\n\n{review}")
    except Exception as e:
        _telegram(f"⚠️ Agente semanal falló: {e}\nStats: {stats}")
