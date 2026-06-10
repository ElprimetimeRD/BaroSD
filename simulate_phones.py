"""Simulador: 20 teléfonos virtuales en el Gran Santo Domingo.

Prueba TODO el pipeline contra tu server de Render sin app todavía.

Uso:
    export BAROSD_API_URL=https://barosd.onrender.com
    export BAROSD_API_KEY=<el API_KEY de Render>
    python simulator/simulate_phones.py            # red tranquila
    python simulator/simulate_phones.py --event    # microrráfaga a los ~3 min
"""
from __future__ import annotations
import argparse, math, os, random, time
import httpx

API_URL = os.environ.get("BAROSD_API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("BAROSD_API_KEY", "dev-key")

N_PHONES, INTERVAL_S = 20, 30
BOUNDS = {"lat_min": 18.38, "lat_max": 18.58, "lon_min": -70.05, "lon_max": -69.75}
EVENT_CENTER, EVENT_RADIUS_KM = (18.472, -69.927), 2.5   # Piantini/Naco
EVENT_START_S, EVENT_PEAK_HPA, EVENT_RAMP_S = 180, 2.2, 240


def hav_km(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * 6371 * math.asin(math.sqrt(x))


class Phone:
    def __init__(self, i):
        self.id = f"sim-{i:02d}"
        self.lat = random.uniform(BOUNDS["lat_min"], BOUNDS["lat_max"])
        self.lon = random.uniform(BOUNDS["lon_min"], BOUNDS["lon_max"])
        self.bias = random.uniform(-2, 2)  # estable, como sensor real

    def pressure(self, t, active, t0):
        p = 1013 + self.bias + 0.4*math.sin(2*math.pi*(t % 86400)/43200) + random.gauss(0, .05)
        if active:
            d = hav_km(self.lat, self.lon, *EVENT_CENTER)
            if d <= EVENT_RADIUS_KM:
                ramp = min(1, max(0, (t - t0)/EVENT_RAMP_S))
                p += EVENT_PEAK_HPA * ramp * max(0, 1 - d/EVENT_RADIUS_KM)
        return round(p, 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", action="store_true")
    ap.add_argument("--minutes", type=float, default=0, help="0 = infinito")
    args = ap.parse_args()
    phones = [Phone(i) for i in range(N_PHONES)]
    t_start = time.time()
    mode = "?"
    print(f"{N_PHONES} teléfonos -> {API_URL}  "
          f"{'CON evento a los 3 min' if args.event else '(red tranquila)'}")
    with httpx.Client(timeout=15) as c:
        while True:
            if args.minutes and time.time() - t_start > args.minutes * 60:
                print("Tiempo cumplido, fin de la simulación."); break
            now = time.time()
            active = args.event and (now - t_start) >= EVENT_START_S
            for ph in phones:
                body = {"ts": now, "p": ph.pressure(now, active, t_start + EVENT_START_S),
                        "lat": ph.lat, "lon": ph.lon, "gps_age": 1}
                try:
                    r = c.post(f"{API_URL}/readings/{ph.id}",
                               headers={"X-Api-Key": API_KEY}, json=body)
                    mode = r.json().get("mode", "?")
                except Exception as e:
                    print(f"[{ph.id}] {e}")
            tag = "EVENTO ACTIVO" if active else "ok"
            print(f"[{int(now - t_start)}s] {tag} | server mode={mode}")
            time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
