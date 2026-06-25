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
```

No versionar `.env`.

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
```

Editar si hace falta:

```bash
sudo nano /etc/systemd/system/binancebot.service
sudo nano /etc/systemd/system/binancebot-guardian.service
```

Si el proyecto no esta en `/opt/BinanceBot`, cambiar estos campos:

- `WorkingDirectory=/opt/BinanceBot`,
- `ExecStart=/opt/BinanceBot/scripts/run_once.sh`,
- `ExecStart=/opt/BinanceBot/.venv/bin/python /opt/BinanceBot/trading/sl_guardian.py`.

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
.venv/bin/python trading/analyze_trades.py
.venv/bin/python trading/analyze_decisions.py
```

## 15. Seguridad

- No ejecutar como root.
- Mantener `.env` con permisos `600`.
- Usar API keys con permisos minimos.
- Restringir IP de API key si Binance y el VPS lo permiten.
- No subir `state.json`, `.env`, logs ni analytics privados a repos publicos.
