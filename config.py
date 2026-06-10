"""Configuración central de BaroSD (versión solo-Render, sin Firebase)."""
import os

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

STORM_MINUTES = 60              # duración del modo tormenta tras una señal
READINGS_RETENTION_H = 48       # cuánto historial crudo guardar
ALERT_COOLDOWN_S = 15 * 60

# --- Zona de interés: Gran Santo Domingo ---
SD_BOUNDS = {
    "lat_min": 18.38, "lat_max": 18.58,
    "lon_min": -70.05, "lon_max": -69.75,
}

METAR_STATION = "MDSD"  # Las Américas, referencia para bias (fase 2)
