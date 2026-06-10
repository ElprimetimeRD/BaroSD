# BaroSD — Red barométrica para detección de microrráfagas en Santo Domingo

Smartphones Android como red de mesoescala + server único en Render
(FastAPI + SQLite en disco persistente, mismo patrón que micron_bot).
**Sin Firebase.**

## Arquitectura

```
[Teléfonos Android / simulador]
   POST /readings/<device_id>  (lote JSON, header X-Api-Key)
   └── la respuesta trae {"mode": "normal"|"storm"}
            │
            ▼
[Render Web Service "barosd"]  ── SQLite en /var/data ──┐
   • detector cada 60 s: tendencia + coherencia espacial │
   • 1ª señal -> modo tormenta 60 min (toda la red       │
     pasa a 1 lectura/2 s automáticamente)               │
   • alertas Telegram 🟡 VIGILANCIA / 🔴 AVISO            │
   • agente semanal (dom 18:00 AST): Claude API analiza  │
     la semana y propone umbrales por Telegram ──────────┘
```

## Ciencia (verificada)

- Barómetros de smartphones: bias absoluto 1–2 hPa pero **estable**; la
  variación es casi idéntica a un barómetro de referencia (σ≈0.02 hPa).
  Detectamos **tendencia**, así el bias se cancela.
- Firma de microrráfaga en superficie: **mesohigh** (couplet de Fujita),
  salto de +0.5–3 hPa en minutos. Umbrales en `server/config.py`.
- Referencia para fase 2 (bias correction estilo uWx, −82% de sesgo):
  METAR MDSD (Las Américas).

## Deploy en Render (10 min, todo desde el móvil)

1. Sube esta carpeta a un repo `barosd` (GitHub móvil).
2. Render → New → Blueprint → conecta el repo (lee `render.yaml`:
   web service + disco de 1 GB).
3. Variables: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (tu bot de siempre).
   `API_KEY` la genera Render sola — cópiala del dashboard.
   `ANTHROPIC_API_KEY` NO es necesaria: sin ella, el reporte semanal te
   llega igual con las estadísticas crudas (gratis, cero costo de API).
   Si algún día recargas créditos, la agregas y el agente pasa a darte
   diagnóstico + propuesta de umbrales.
4. Al iniciar te llega "🌀 BaroSD server iniciado" por Telegram.
5. `https://barosd.onrender.com/` muestra el estado de la red.

## Probar HOY sin app — 100% desde el celular

No necesitas terminal ni PC: el simulador corre en GitHub Actions.
1. Repo → Settings → Secrets → Actions: `BAROSD_API_URL` y `BAROSD_API_KEY`.
2. Pestaña **Actions** → "Probar red (simulador)" → **Run workflow**
   (evento = true, 12 minutos).
3. A los ~4 min de iniciado: 🔴 AVISO por Telegram con epicentro en
   Piantini/Naco, y en el log del workflow verás `server mode=storm`.

## APK sin PC (GitHub Actions)

App en `/android`: Kotlin puro, cero dependencias, APK < 1 MB.
1. Repo → Settings → Secrets → Actions: `BAROSD_API_URL` y `BAROSD_API_KEY`
   (los mismos dos valores de arriba).
2. Actions → "Build APK" → Run workflow (~3 min).
3. Descarga el artifact `barosd-apk` al teléfono e instala.
4. Abrir BaroSD → Iniciar monitoreo → aceptar permisos. El teléfono ya
   aporta lecturas idénticas a las del simulador.

Requisito: barómetro integrado (Galaxy S/Note, Pixel y la mayoría de
gama media-alta; la app lo verifica sola).

## Fases

| Fase | Qué | Estado |
|------|-----|--------|
| 0 | Server + detector + simulador | ✅ hoy |
| 1 | App Android + CI de APK | ✅ hoy |
| 2 | Piloto 20–50 teléfonos (EGE Haina / SITES), bias vs MDSD | reclutamiento |
| 3 | ML de bias, dashboard público, ONAMET | después |

## Endpoints

- `POST /readings/{device_id}` — lote o lectura única `{ts,p,lat,lon,gps_age}`,
  header `X-Api-Key`. Respuesta incluye `mode`.
- `GET /config` — `{"mode": "normal"|"storm", "until": epoch}`
- `GET /` — estado: dispositivos/lecturas 24 h, último evento, modo.
