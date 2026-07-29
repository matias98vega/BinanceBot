# Instrucciones de BinanceBot

## Principios

- Realizá cambios incrementales, conservadores, reversibles y observables.
- Preservá los cambios ajenos presentes en el working tree y no incluyas archivos fuera del alcance.
- No amplíes el alcance sin una solicitud explícita.
- Ante bugs o estados inconsistentes, investigá y reuní evidencia antes de modificar.
- No declares una tarea terminada sin evidencia reciente de las verificaciones aplicables.

## Seguridad de producción

Sin autorización explícita, no:

- ejecutes órdenes, transferencias, rebalances, depósitos ni retiros reales;
- modifiques credenciales o secretos;
- imprimas secretos, firmas ni payloads autenticados;
- modifiques systemd o sudoers;
- reinicies servicios, timers, Guardian ni Dashboard;
- uses `sudo`.

## Telegram

La única excepción técnica autorizada para `sudo` es:

```text
sudo -n /usr/local/sbin/binancebot-restart-telegram
```

Ejecutala solamente ante una solicitud explícita de reinicio o despliegue de Telegram. Después, comprobá `ActiveState`, `SubState` y `Result`; revisá `journalctl -u binancebot-telegram.service`; y confirmá que el servicio quede `active/running`.

## Git

- Hacé commit únicamente cuando la tarea lo solicite o el workflow acordado lo requiera.
- Hacé push únicamente ante una solicitud explícita.
- Antes de commit o push, completá las verificaciones aplicables.
- Antes de push, comprobá rama, destino y working tree.
- No incluyas archivos ajenos al alcance.

## Datos

- No borres, reescribas ni backfillees históricos sin una tarea específica y un procedimiento de seguridad.
- No uses archivos productivos como fixtures mutables.
- Evitá que pruebas o scripts alteren estado, históricos o datos runtime reales.

## Documentación de referencia

Consultá sin duplicar su contenido:

- `README.md`
- `ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/DESIGN_NOTES.md`
- `docs/ROADMAP.md`
- `docs/BACKLOG.md`
- `docs/VERSIONING_POLICY.md`
- `docs/CHANGELOG.md`

No copies en este archivo arquitectura, roadmap, historial de incidentes ni conteos fijos de tests.
