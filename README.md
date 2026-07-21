# BinanceBot

Versión runtime vigente: `v1.2-sizing-v2`.

Bot de trading para Binance con ejecución local/VPS, telemetría JSONL, validaciones de observabilidad, dashboard local y ejemplos de despliegue con `systemd`.

Capacidades desplegadas destacadas:

- Long Spot y Short Futures con Guardian, TP/SL, partials, trailing y rebalance.
- Sizing v2 por exposición y slots.
- Analytics, Insights, Trade Inspector y Timeline read-only.
- Telegram con capital real/usado/libre, PnL abierto, estadísticas y diagnósticos por versión.
- Reconciliación de posiciones Spot stale, Futures observadas y rebalance pendiente alineado.
- Capital ledger schema v2, flujos externos separados y contabilidad desde el bootstrap productivo.

## Requisitos

- Python 3.10 o superior.
- Cuenta Binance con API Key y Secret.
- Git para despliegue en VPS.
- Ubuntu 24.04 LTS recomendado para ejecucion 24/7.

El proyecto usa solo librerias de la biblioteca estandar de Python para el bot y el dashboard. `requirements.txt` queda como referencia de instalacion.

## Instalacion local

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Completar `.env`:

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
BOT_TOTAL_CAPITAL_LIMIT_USDT=50
# Deprecated temporalmente:
BOT_SPOT_CAPITAL_LIMIT_USDT=50
BOT_FUTURES_CAPITAL_LIMIT_USDT=25
BOT_MAX_EXPOSURE_PERCENT=80
REBALANCE_MIN_WALLET_USDT=0
# Deprecated; no participa en sizing ni validacion. Eliminar de .env:
# BOT_MAX_POSITION_PERCENT=20
```

No versionar `.env`.

`BOT_TOTAL_CAPITAL_LIMIT_USDT` es la fuente principal de capital autorizado. Si el capital real es menor al limite configurado, el bot usa el capital real disponible y registra una advertencia suave. Las variables Spot/Futures separadas quedan temporalmente como deprecated para guardrails existentes. El maximo por operacion se calcula con `BOT_MAX_EXPOSURE_PERCENT / max_positions`.

`REBALANCE_MIN_WALLET_USDT` controla una reserva minima opcional por wallet durante rebalanceos. El default recomendado es `0` para permitir mover el 100% del capital a la wallet objetivo; subirlo, por ejemplo a `3`, conserva ese saldo minimo.

## Checks antes de operar

```bash
python trading/setup_check.py
python trading/preflight_check.py
python trading/post_cycle_check.py --save-baseline
BINANCEBOT_TEST_MODE=true BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS=true python -m unittest discover trading
```

`setup_check.py` verifica Python, dependencias, `.env`, archivos, permisos, ping HTTPS a Binance y autenticacion de API. No abre operaciones.
Tambien valida el limite total de capital y muestra capital real vs capital autorizado.

`preflight_check.py` ejecuta healthcheck, validacion de observabilidad y analizadores locales.

## Ejecucion

Una iteracion manual:

```bash
python trading/bot.py
python trading/post_cycle_check.py
```

Guardian SL manual:

```bash
python trading/sl_guardian.py
```

## Analytics y observabilidad

```bash
python trading/analyze_trades.py
python trading/analyze_decisions.py
python trading/analyze_version_performance.py
python trading/analyze_short_performance.py
python trading/analyze_capital_accounting.py --explain
python trading/audit_data_quality.py
python trading/validate_observability.py
python trading/healthcheck.py
python trading/analytics.py --export
```

Diagnostico de cuenta Binance de solo lectura:

```bash
python tools/account_diagnostic.py
```

Archivos principales generados localmente:

- `trading/state.json`
- `trading/trades_log.txt`
- `trading/trade_analytics.jsonl`
- `trading/decision_snapshots.jsonl`
- `trading/reports/trades.csv`

Estos archivos contienen estado/datos operativos y no deben versionarse.

### Capital ledger y contabilidad

El ledger append-only vive en `data/history/capital_ledger.jsonl` y usa schema v2. La convención vigente considera `REALIZED_PNL` neto de trading fees, registra `TRADING_FEE` sólo como información y suma `FUNDING_FEE` con su signo.

El bootstrap productivo fija el inicio contable en `2026-07-20T21:47:14Z`. PnL Trading y ROI Trading excluyen actividad anterior; no deben interpretarse como rendimiento histórico completo del bot. La observación actual de uPnL Spot/Futures es read-only y falla explícitamente si faltan precios o existe un mismatch de cantidad.

## Dashboard local

Ejecutar:

```bash
python dashboard/app.py
```

Abrir:

```text
http://127.0.0.1:8080
```

Endpoints:

- `/api/status`
- `/api/trades`
- `/api/snapshots`
- `/api/health`
- `/api/metrics`

El dashboard solo lee archivos locales y no se conecta a Binance.

## Despliegue Ubuntu 24.04

La guia completa esta en:

```text
docs/DEPLOY_UBUNTU.md
```

Resumen:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates
cd /opt
sudo git clone <repo-url> BinanceBot
sudo chown -R binancebot:binancebot /opt/BinanceBot
cd /opt/BinanceBot
chmod +x scripts/*.sh
./scripts/install_ubuntu.sh
.venv/bin/python trading/setup_check.py
./scripts/preflight.sh
./scripts/post_cycle.sh --save-baseline
```

Los scripts en `scripts/` detectan automaticamente la raiz del proyecto desde su propia ubicacion. No dependen de `/opt/BinanceBot`.

## systemd

Ejemplos incluidos:

- `deploy/systemd/binancebot.service`
- `deploy/systemd/binancebot.timer`
- `deploy/systemd/binancebot-guardian.service`
- `deploy/systemd/binancebot-guardian.timer`

Las unidades systemd usan `/opt/BinanceBot` como ejemplo. Si el repo esta en otra ruta, editar `WorkingDirectory` y `ExecStart` en los archivos `.service`.

Bot cada 2 minutos:

```bash
sudo cp deploy/systemd/binancebot.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binancebot.timer
```

Guardian cada 1 minuto:

```bash
sudo cp deploy/systemd/binancebot-guardian.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binancebot-guardian.timer
```

Logs:

```bash
journalctl -u binancebot.service -f
journalctl -u binancebot-guardian.service -f
```

## Evolución estadística y ML

La secuencia aprobada es estrictamente progresiva: auditoría formal del dataset, baseline estadístico reproducible, walk-forward sin ML, XGBoost offline, shadow mode read-only y sólo después una posible evaluación de veto conservador. Ningún componente ML actual modifica scoring, sizing u órdenes; el sizing adaptativo permanece bloqueado para una fase futura independiente. Ver `docs/ROADMAP.md`.

## Documentacion

Documentos principales:

- `ARCHITECTURE.md`: arquitectura canonica y flujos.
- `docs/MODULES.md`: guia por modulo.
- `docs/ROADMAP.md`: estado actual, prioridades y deuda tecnica.
- `docs/DESIGN_NOTES.md`: decisiones de diseno y tradeoffs.
- `docs/BACKLOG.md`: trabajo pendiente clasificado.
- `docs/CHANGELOG.md`: hitos importantes.
- `docs/FUTURE_VISION.md`: vision de evolucion futura.
- `docs/DEPLOY_UBUNTU.md`: despliegue Ubuntu/systemd.

## Estructura

```text
dashboard/          Dashboard local HTML/CSS/JS y API http.server
deploy/systemd/     Unidades systemd de ejemplo
docs/               Documentacion tecnica y despliegue
scripts/            Wrappers operativos para Ubuntu/VPS
trading/            Bot, estrategia, observabilidad y analytics
ARCHITECTURE.md     Arquitectura canonica
README.md           Guia principal
requirements.txt    Dependencias Python
.env.example        Variables de entorno sin secretos
VERSION             Version runtime vigente
```

## Seguridad

- `.env` y `trading/.env` estan ignorados.
- Logs, caches, `state.json`, JSONL operativos y reportes generados estan ignorados.
- Usar API keys con permisos minimos y restriccion por IP si es posible.
- No ejecutar el bot como `root` en VPS.
