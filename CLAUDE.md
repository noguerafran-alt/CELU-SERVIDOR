# CELU-SERVIDOR — contrato del repo

Infraestructura del celular servidor (Samsung Galaxy A22, Android 13, Termux) que en casa corre LUBRICANTES
(`~/lub-casa`, servicio `lub-server` en 127.0.0.1:8090, publicado por `lub-tunel` en lubricantes.fln-data-analysis.com) y CLIPPER.
Salió de LUBRICANTES el 2026-11-22: lo que mide el equipo no es del negocio de lubricantes.

## Qué hay
- `celu.py` — servicio `celu-servidor` (runit) en **127.0.0.1:8092** (`CELU_PUERTO`; nunca 0.0.0.0). Un solo endpoint:
  `GET /salud` → `{"ok": true, "equipo": {bateria, disco_gb, server_arriba_s, equipo_arriba_s, trafico_24h, clipper, red, ram_mb, cpu, procesos}}`.
  - `server_arriba_s` = segundos de `lub-server` según `sv status` (ya no el proceso que mide).
  - `trafico_24h` = contado del log de LUBRICANTES (`~/lub-casa/logs/server.log`, una línea por pedido de http.server, hora local); `mb` e `ips` son `null` porque el log no los trae.
  - Lo que no se puede leer es `null`, nunca un 500. **Sin secretos**: nunca IPs, env, rutas, argumentos ni claves (asserts en `demo()`).
- **Vigía** (hilo de `celu.py`, cada `CELU_VIGIA_MIN` minutos, default 1): si `~/lub-casa/server.py` cambió hace más de 30 s y compila → `sv restart lub-server`.
  Es lo que redeploya LUBRICANTES en el celu tras el pull horario. Si cambia `celu.py`, sale (`os._exit(0)`) y runit lo levanta con el código nuevo.
- `termux_setup.sh` — instala / actualiza todo el celu (paquetes, clon `~/lub-casa`, cron, servicios `lub-server`, `lub-tunel`, `celu-servidor`, Termux:Boot). `estado` = diagnóstico.
- `docs/celular.md` — guía completa del celu (túnel, tokens, SEPA archivo, adb, qué mide `/salud`).

## Reglas
- Python 3 solo stdlib. Todo en español. Comentarios solo para el porqué.
- Lógica nueva → su `assert` en `demo()` (`python celu.py test` = `OK`).
- El celu tiene este repo en `~/celu-servidor` y lo actualiza con `git pull --ff-only` cada hora (minuto 25, por SSH con una deploy key de solo lectura, alias `github-celu` en `~/.ssh/config`).
- No exponer este servicio a internet sin pensarlo: hoy solo escucha en loopback. Para verlo desde afuera: `ssh celu-afuera curl -s 127.0.0.1:8092/salud`.
