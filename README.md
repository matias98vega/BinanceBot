# BinanceBot

Version candidata: `v1.0-alpha`.

Bot de trading para Binance con ejecucion local/VPS, telemetria JSONL, validaciones de observabilidad, dashboard local y ejemplos de despliegue con `systemd`.

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
BOT_SPOT_CAPITAL_LIMIT_USDT=50
BOT_FUTURES_CAPITAL_LIMIT_USDT=25
BOT_MAX_POSITION_PERCENT=20
BOT_MAX_EXPOSURE_PERCENT=80
```

No versionar `.env`.

Los limites de capital son obligatorios. El bot calcula el tamano de orden con la logica existente, pero antes de enviar una orden valida que no supere el capital autorizado, el maximo por posicion y la exposicion maxima configurada.

## Checks antes de operar

```bash
python trading/setup_check.py
python trading/preflight_check.py
python trading/post_cycle_check.py --save-baseline
```

`setup_check.py` verifica Python, dependencias, `.env`, archivos, permisos, ping HTTPS a Binance y autenticacion de API. No abre operaciones.
Tambien valida los limites de capital y muestra capital real vs capital usable para Spot/Futures.

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

## Estructura

```text
dashboard/          Dashboard local HTML/CSS/JS y API http.server
deploy/systemd/     Unidades systemd de ejemplo
docs/               Documentacion de despliegue
scripts/            Wrappers operativos para Ubuntu/VPS
trading/            Bot, estrategia, observabilidad y analytics
README.md           Guia principal
requirements.txt    Dependencias Python
.env.example        Variables de entorno sin secretos
VERSION             Version candidata
```

## Seguridad

- `.env` y `trading/.env` estan ignorados.
- Logs, caches, `state.json`, JSONL operativos y reportes generados estan ignorados.
- Usar API keys con permisos minimos y restriccion por IP si es posible.
- No ejecutar el bot como `root` en VPS.
