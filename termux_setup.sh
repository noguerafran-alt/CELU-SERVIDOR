#!/data/data/com.termux/files/usr/bin/bash
# Prepara el celular (Termux) como recolector de casa Y servidor de LUBRICANTES, más el estado del equipo (CELU-SERVIDOR):
# paquetes, clon dedicado, marcador, crontab (recolección 06:00, pull de LUBRICANTES y de CELU-SERVIDOR cada hora),
# servicios sv (lub-server, lub-tunel, celu-servidor) y arranque tras reinicio.
# NO pide ni guarda el token de GitHub ni el de Cloudflare: eso lo hacés vos (ver docs/celular.md).
# Se puede correr varias veces: no duplica nada.
#   bash ~/celu-servidor/termux_setup.sh          instala / actualiza todo
#   bash ~/celu-servidor/termux_setup.sh estado   muestra qué está andando (no cambia nada)
set -e

REPO="${LUB_REPO:-https://github.com/noguerafran-alt/LUBRICANTES.git}"
CLON="$HOME/lub-casa"
CELU="$(cd "$(dirname "$0")" && pwd)"   # este repo (CELU-SERVIDOR), clonado en ~/celu-servidor
CELU_PUERTO="${CELU_PUERTO:-8092}"      # 8090 = LUBRICANTES, 8091 = panel de CLIPPER
ZONA="America/Argentina/Buenos_Aires"
PUERTO="${LUB_PUERTO:-8090}"   # fijo y siempre en 127.0.0.1: el túnel de Cloudflare es lo único que lo ve desde afuera
PREFIJO="${PREFIX:-/data/data/com.termux/files/usr}"
SERVICIOS="$PREFIJO/var/service"

estado() {
  echo "== crond";      if pgrep crond >/dev/null; then echo "andando"; else echo "NO anda (probá: crond)"; fi
  echo "== lub-server"; sv status lub-server 2>&1 || true
  echo -n "   /salud: "; curl -fsS -m 5 "http://127.0.0.1:$PUERTO/salud" 2>&1 || echo "no responde en 127.0.0.1:$PUERTO"
  echo; echo "== lub-tunel";  sv status lub-tunel 2>&1 || true
  echo "== celu-servidor"; sv status celu-servidor 2>&1 || true
  echo -n "   /salud: "; curl -fsS -m 10 "http://127.0.0.1:$CELU_PUERTO/salud" 2>&1 | head -c 300 || echo "no responde en 127.0.0.1:$CELU_PUERTO"
  echo; echo "   HEAD: $(git -C "$CELU" log -1 --format='%h %cd %s' --date=iso 2>/dev/null)"
  echo "== ultima corrida casa (fuentes.csv)"; grep ',casa,' "$CLON/data/fuentes.csv" 2>/dev/null | tail -1 || true
  echo "   log: $(grep '^=== ' "$CLON/recolector_casa.log" 2>/dev/null | tail -1)"
  echo "== ultimo pull"; echo "   log: $(grep '^=== ' "$CLON/pull.log" 2>/dev/null | tail -1)"
  echo "   HEAD: $(git -C "$CLON" log -1 --format='%h %cd %s' --date=iso 2>/dev/null)"
}

if [ "${1:-}" = "estado" ]; then estado; exit 0; fi

# el mirror al azar puede dejar índices sin bajar ("Some index files failed to download"): se fija el de por defecto
if [ ! -e "$PREFIJO/etc/termux/chosen_mirrors" ]; then
  ln -s "$PREFIJO/etc/termux/mirrors/default" "$PREFIJO/etc/termux/chosen_mirrors"
fi
# upgrade completo y no solo update: con libcurl nuevo y openssl viejo git https no linkea (SSL_set_quic_tls_early_data_enabled)
# sin tzdata: no existe en Termux; el código usa UTC-3 fijo y CRON_TZ funciona con las zonas de Android
yes | pkg upgrade -y
pkg install -y python git cronie termux-services cloudflared curl

if [ ! -d "$CLON/.git" ]; then
  git clone "$REPO" "$CLON"
fi
touch "$CLON/.recolector_casa"   # marcador: el script hace reset --hard y solo corre en este clon
mkdir -p "$CLON/logs"

# crontab: recolección 06:00 hora argentina y pull liviano cada hora en el minuto 20 (se sacan las líneas viejas si ya estaban)
( crontab -l 2>/dev/null | grep -v -e recolector_casa -e '^CRON_TZ=' -e '^SEPA_ARCHIVO=' -e celu-servidor || true
  echo "25 * * * * git -C $CELU pull -q --ff-only >> $CELU/logs/pull.log 2>&1   # celu-servidor: el vigía de celu.py lo relanza si cambió"
  echo "CRON_TZ=$ZONA"
  echo "SEPA_ARCHIVO=$HOME/sepa-archivo"   # zips crudos de SEPA con rotación, fuera del clon (que hace reset --hard)
  echo "0 6 * * * python $CLON/tools/recolector_casa.py >> $CLON/cron.log 2>&1"
  echo "20 * * * * python $CLON/tools/recolector_casa.py --solo-pull >> $CLON/logs/pull-cron.log 2>&1" ) | crontab -

# servicio lub-server: python server.py solo en loopback (el server nunca usa 0.0.0.0 fuera de Render; sin RENDER en el entorno)
mkdir -p "$SERVICIOS/lub-server" "$SERVICIOS/lub-tunel"
cat > "$SERVICIOS/lub-server/run" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
cd "@CLON@" || exit 1
unset RENDER
export PORT=@PUERTO@ LUB_DATOS="@CLON@"
exec python server.py >> "@CLON@/logs/server.log" 2>&1
EOF

# servicio lub-tunel: dos variantes (la del login es la recomendada; ver docs/celular.md). El token va por entorno, no por la línea de comandos.
cat > "$SERVICIOS/lub-tunel/run" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
LOG="@CLON@/logs/tunel.log"
if [ -f "$HOME/.lub-tunel-token" ]; then
  export TUNNEL_TOKEN="$(cat "$HOME/.lub-tunel-token")"
  exec cloudflared tunnel --no-autoupdate run >> "$LOG" 2>&1
elif [ -f "$HOME/.cloudflared/cert.pem" ]; then
  exec cloudflared tunnel --no-autoupdate run --url http://127.0.0.1:@PUERTO@ lubricantes >> "$LOG" 2>&1
fi
echo "$(date) falta el login de cloudflared o ~/.lub-tunel-token (docs/celular.md); reintento en 5 min" >> "$LOG"
sleep 300
exit 1
EOF
# servicio celu-servidor: estado del equipo en loopback y vigía que reinicia lub-server cuando el pull trae otro server.py
mkdir -p "$SERVICIOS/celu-servidor" "$CELU/logs"
cat > "$SERVICIOS/celu-servidor/run" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
cd "@CELU@" || exit 1
export CELU_PUERTO=@CELU_PUERTO@ LUB_CLON="@CLON@"
exec nice -n 5 python celu.py >> "@CELU@/logs/celu.log" 2>&1
EOF
sed -i "s|@CLON@|$CLON|g; s|@PUERTO@|$PUERTO|g; s|@CELU@|$CELU|g; s|@CELU_PUERTO@|$CELU_PUERTO|g" "$SERVICIOS/lub-server/run" "$SERVICIOS/lub-tunel/run" "$SERVICIOS/celu-servidor/run"
chmod +x "$SERVICIOS/lub-server/run" "$SERVICIOS/lub-tunel/run" "$SERVICIOS/celu-servidor/run"
rm -f "$SERVICIOS/lub-server/down" "$SERVICIOS/lub-tunel/down" "$SERVICIOS/celu-servidor/down"   # habilitados: arrancan con runsvdir

# arranque tras reinicio (necesita la app Termux:Boot instalada y abierta una vez)
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/recolector-casa.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
crond
# runsvdir (termux-services) levanta lub-server y lub-tunel
. "${PREFIX:-/data/data/com.termux/files/usr}/etc/profile.d/start-services.sh"
EOF
chmod +x "$HOME/.termux/boot/recolector-casa.sh"

# ahora mismo
termux-wake-lock || true
pgrep crond >/dev/null || crond
. "$PREFIJO/etc/profile.d/start-services.sh" || true
sleep 6   # runsvdir revisa el directorio de servicios cada ~5 s

echo
estado
echo
echo "Listo. Faltan, a mano (docs/celular.md): el token de GitHub (para publicar) y el túnel de Cloudflare (cloudflared tunnel login/create/route dns)."
echo "Prueba de la recolección:  cd $CLON && python tools/recolector_casa.py --sin-push"
echo "Hora de Termux ahora: $(date)  (si no es la de Argentina no pasa nada: el cron usa CRON_TZ y el script fecha en hora argentina)"
