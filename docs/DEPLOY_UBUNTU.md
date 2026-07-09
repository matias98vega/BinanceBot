# Deploy Ubuntu 24.04 LTS

Guia para ejecutar BinanceBot 24/7 en Ubuntu 24.04 LTS con `systemd timer`.

No cambia estrategia, filtros ni parametros de trading. Solo describe despliegue operativo.

## 1. Crear VPS

1. Crear un VPS Ubuntu 24.04 LTS.
2. Elegir una region cercana y estable.
3. Activar firewall del proveedor si esta disponible.
4. Conectarse por SSH:

```bash
ssh root@IP_DEL_VPS
```

## 2. Crear usuario no-root

```bash
adduser binancebot
usermod -aG sudo binancebot
su - binancebot
```

Trabajar desde este usuario para evitar ejecutar el bot como `root`.

## 3. Instalar Python, Git y herramientas base

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates
```

Verificar:

```bash
python3 --version
git --version
```

## 4. Clonar repo

Ejemplo:

```bash
cd /opt
sudo git clone REPO_URL BinanceBot
sudo chown -R binancebot:binancebot /opt/BinanceBot
cd /opt/BinanceBot
```

Si se copia manualmente, mantener el proyecto en:

```text
/opt/BinanceBot
```

Los scripts en `scripts/` no dependen de esa ruta: detectan automaticamente la raiz del proyecto desde su propia ubicacion. `/opt/BinanceBot` se usa solo como ruta recomendada y en los ejemplos de `systemd`.

## 5. Crear venv

```bash
cd /opt/BinanceBot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 6. Crear `.env`

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Completar:

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_SPOT_BASE=https://api.binance.com
BINANCE_FUTURES_BASE=https://fapi.binance.com
LOCK_FILE=/tmp/trading_bot.lock
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

`BOT_TOTAL_CAPITAL_LIMIT_USDT` es obligatorio y debe ser mayor a cero. `setup_check.py` queda NOT READY si falta o es invalido. Si el capital real de la subcuenta es menor al limite configurado, no bloquea: usa el capital real disponible y muestra una advertencia suave. `BOT_SPOT_CAPITAL_LIMIT_USDT` y `BOT_FUTURES_CAPITAL_LIMIT_USDT` quedan deprecated temporalmente para compatibilidad con guardrails existentes. El maximo por operacion se calcula con `BOT_MAX_EXPOSURE_PERCENT / max_positions`.

`REBALANCE_MIN_WALLET_USDT` controla una reserva minima opcional por wallet durante rebalanceos. El default recomendado es `0` para permitir mover el 100% del capital a la wallet objetivo; subirlo, por ejemplo a `3`, conserva ese saldo minimo.

## 7. Ejecutar setup check

```bash
cd /opt/BinanceBot
./scripts/preflight.sh
.venv/bin/python trading/setup_check.py
```

Esperado para despliegue completo:

```text
Final Status ...... READY
```

## 8. Ejecutar preflight check

```bash
cd /opt/BinanceBot
./scripts/preflight.sh
```

Si el warning viene de campos `null` historicos recuperados, no bloquea por si solo. Si hay `ERROR`, revisar antes de operar.

## 9. Guardar baseline

```bash
cd /opt/BinanceBot
./scripts/post_cycle.sh --save-baseline
```

## 10. Ejecutar una iteracion manual

```bash
cd /opt/BinanceBot
./scripts/run_once.sh
./scripts/post_cycle.sh
```

Revisar que:

- `state analytics aligned: True`,
- `new corrupt JSONL lines: 0`,
- snapshots aumenten cuando hubo escaneo,
- analytics refleje aperturas/cierres.

## 11. Instalar systemd timer

Copiar unidades:

```bash
sudo cp deploy/systemd/binancebot.service /etc/systemd/system/
sudo cp deploy/systemd/binancebot.timer /etc/systemd/system/
sudo cp deploy/systemd/binancebot-guardian.service /etc/systemd/system/
sudo cp deploy/systemd/binancebot-guardian.timer /etc/systemd/system/
sudo cp deploy/systemd/binancebot-dashboard.service /etc/systemd/system/
sudo cp deploy/systemd/binancebot-telegram.service /etc/systemd/system/
```

Editar si hace falta:

```bash
sudo nano /etc/systemd/system/binancebot.service
sudo nano /etc/systemd/system/binancebot-guardian.service
sudo nano /etc/systemd/system/binancebot-dashboard.service
sudo nano /etc/systemd/system/binancebot-telegram.service
```

Si el proyecto no esta en `/opt/BinanceBot`, cambiar estos campos:

- `WorkingDirectory=/opt/BinanceBot`,
- `ExecStart=/opt/BinanceBot/scripts/run_once.sh`,
- `ExecStart=/opt/BinanceBot/.venv/bin/python /opt/BinanceBot/trading/sl_guardian.py`,
- `ExecStart=/opt/BinanceBot/.venv/bin/python /opt/BinanceBot/dashboard/app.py`,
- `ExecStart=/opt/BinanceBot/.venv/bin/python /opt/BinanceBot/trading/telegram_commands.py`.

Tambien verificar:

- `User=binancebot`,
- `Group=binancebot`.

Recargar systemd:

```bash
sudo systemctl daemon-reload
```

Activar timers:

```bash
sudo systemctl enable --now binancebot.timer
sudo systemctl enable --now binancebot-guardian.timer
sudo systemctl enable --now binancebot-dashboard.service
sudo systemctl enable --now binancebot-telegram.service
```

## 12. Timers propuestos

### Bot

- Unidad: `binancebot.service`
- Timer: `binancebot.timer`
- Frecuencia: cada 2 minutos.
- Concurrencia: si ya hay una instancia activa, `systemd` no lanza otra ejecucion del mismo service; adicionalmente el bot mantiene lock propio por `LOCK_FILE`.
- Logs: journalctl.

### Guardian

- Unidad: `binancebot-guardian.service`
- Timer: `binancebot-guardian.timer`
- Frecuencia propuesta: cada 1 minuto.
- Motivo: `sl_guardian.py` es liviano y protege SLs con mayor frecuencia que el ciclo principal.
- Logs: journalctl.

## 13. Comandos utiles

Estado timers:

```bash
systemctl status binancebot.timer
systemctl status binancebot-guardian.timer
```

Estado servicios:

```bash
systemctl status binancebot.service
systemctl status binancebot-guardian.service
```

Logs del bot:

```bash
journalctl -u binancebot.service -n 100 --no-pager
journalctl -u binancebot.service -f
```

Logs guardian:

```bash
journalctl -u binancebot-guardian.service -n 100 --no-pager
journalctl -u binancebot-guardian.service -f
```

Logs dashboard:

```bash
journalctl -u binancebot-dashboard.service -n 100 --no-pager
journalctl -u binancebot-dashboard.service -f
```

Logs Telegram:

```bash
journalctl -u binancebot-telegram.service -n 100 --no-pager
journalctl -u binancebot-telegram.service -f
```

Detener timers:

```bash
sudo systemctl stop binancebot.timer
sudo systemctl stop binancebot-guardian.timer
```

Desactivar timers:

```bash
sudo systemctl disable binancebot.timer
sudo systemctl disable binancebot-guardian.timer
```

Ejecutar una vez manualmente:

```bash
cd /opt/BinanceBot
./scripts/run_once.sh
./scripts/post_cycle.sh
```

Ejecutar servicio manualmente:

```bash
sudo systemctl start binancebot.service
sudo systemctl start binancebot-guardian.service
sudo systemctl start binancebot-dashboard.service
sudo systemctl start binancebot-telegram.service
```

Ver ultimas ejecuciones:

```bash
systemctl list-timers | grep binancebot
```

## 14. Revision de logs y archivos

Archivos locales:

```bash
tail -n 100 trading/trades_log.txt
tail -n 20 trading/trade_analytics.jsonl
tail -n 5 trading/decision_snapshots.jsonl
```

Checks:

```bash
./scripts/preflight.sh
./scripts/post_cycle.sh
BINANCEBOT_TEST_MODE=true BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS=true .venv/bin/python -m unittest discover trading
.venv/bin/python trading/analyze_trades.py
.venv/bin/python trading/analyze_decisions.py
```

## 15. Dashboard local como servicio

El dashboard se ejecuta solo en loopback por defecto:

```ini
Environment=DASHBOARD_HOST=127.0.0.1
Environment=DASHBOARD_PORT=8080
```

Instalar y probar:

```bash
sudo cp deploy/systemd/binancebot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binancebot-dashboard.service
curl http://127.0.0.1:8080/api/status
```

No abrir el puerto `8080` al publico. El dashboard lee estado local del bot y debe quedar accesible solo por SSH tunnel, VPN o reverse proxy protegido.

## 16. Dashboard detras de Nginx/HTTPS

No implementar esto hasta necesitar acceso remoto. Diseno recomendado:

- Mantener `DASHBOARD_HOST=127.0.0.1`.
- Nginx escucha en `443` y hace proxy a `http://127.0.0.1:8080`.
- Activar Basic Auth en Nginx.
- Usar HTTPS con Certbot.
- No exponer Flask/http.server directo a Internet.
- No abrir `8080` en firewall publico.

## 17. Telegram comandos solo lectura

Configurar en `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Instalar servicio:

```bash
sudo cp deploy/systemd/binancebot-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binancebot-telegram.service
```

Probar desde el chat autorizado:

```text
/status
/help
/menu
```

Comandos disponibles:

Estado:

- `/status`
- `/health`
- `/capital`
- `/positions`

Trading:

- `/lasttrades`
- `/snapshots`

Sistema:

- `/help`
- `/menu`

Proximamente:

- `/pnl`
- `/stats`
- `/logs`
- `/version`

`/menu` muestra una botonera inline para ejecutar consultas desde el celular sin escribir comandos. Los botones disponibles son Estado, Health, Capital, Posiciones, Ultimos trades, Snapshots y Ayuda.

El servicio es solo lectura: no abre ordenes, no cierra ordenes, no pausa/reanuda y no modifica `state.json`. Solo escribe `trading/telegram_offset.json` para recordar el ultimo update procesado.

## 18. Seguridad

- No ejecutar como root.
- Mantener `.env` con permisos `600`.
- Usar API keys con permisos minimos.
- Restringir IP de API key si Binance y el VPS lo permiten.
- No subir `state.json`, `.env`, logs ni analytics privados a repos publicos.
- No publicar `TELEGRAM_BOT_TOKEN` ni `TELEGRAM_CHAT_ID`.
