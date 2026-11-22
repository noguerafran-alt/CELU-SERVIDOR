"""CELU-SERVIDOR: estado del celular servidor (Galaxy A22, Termux) y el vigía que redeploya LUBRICANTES.

Sirve en 127.0.0.1 (CELU_PUERTO, default 8092) un solo JSON, GET /salud -> {"ok", "equipo": {...}}: batería, disco, RAM/CPU,
procesos, red, CLIPPER, uptime del equipo y de lub-server, y el tráfico de las últimas 24 h de LUBRICANTES (leído de su log).
Sin secretos: nunca IPs, env, rutas, argumentos ni claves.
El vigía (hilo): si cambia ~/lub-casa/server.py (pull horario de LUBRICANTES), lo compila y hace `sv restart lub-server`;
si cambia este archivo, sale y runit lo levanta con el código nuevo.

Uso: python celu.py | python celu.py test
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
LUB = Path(os.environ.get("LUB_CLON", HOME / "lub-casa"))
LUB_LOG = LUB / "logs" / "server.log"
ARCHIVO = Path(__file__).resolve()
EN_MAIN = False  # lo pone main(): en los tests el vigía nunca reinicia nada


_BAT = [0.0, None]  # [cuándo, valor]: caché de 60 s, también del fallo


def _leer_bateria():
    import shutil
    if shutil.which("termux-battery-status"):
        try:
            p = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=5)
            j = json.loads(p.stdout)
            return {"pct": j.get("percentage"), "estado": j.get("status"), "temp_c": j.get("temperature")}
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
    d = Path("/sys/class/power_supply/battery")
    try:
        t = (d / "temp").read_text().strip()
        return {"pct": int((d / "capacity").read_text()), "estado": (d / "status").read_text().strip(),
                "temp_c": int(t) / 10 if t else None}
    except (OSError, ValueError):
        return None


def bateria():
    if time.time() - _BAT[0] > 60:
        _BAT[1] = _leer_bateria()
        _BAT[0] = time.time()
    return _BAT[1]


_INTERPRETES = ("sh", "bash", "node")
_RC = {"lk": threading.Lock(), "prev": None, "cache": (0.0, None)}  # prev = última muestra; cache = (cuándo, resultado) de 5 s


def _sc(nombre, defecto):
    try:
        return os.sysconf(nombre)
    except (AttributeError, ValueError, OSError):
        return defecto


def _nombre_proc(comm, cmdline):
    """Solo un nombre corto: el script si es un intérprete, si no el comm. Nunca rutas ni argumentos."""
    if comm in _INTERPRETES or comm.startswith("python"):
        for a in cmdline.split("\0")[1:]:
            if a.endswith((".py", ".sh", ".js")):
                return os.path.basename(a.replace("\\", "/"))[:40]
    return comm[:40]


def _muestra(proc="/proc", uid=None, pagina=None):
    """(t monotónico, ticks totales de /proc/stat o None, {pid: (nombre, ticks utime+stime, rss en bytes)}) de los procesos de `uid`."""
    pagina = pagina or _sc("SC_PAGE_SIZE", 4096)
    proc = Path(proc)
    total = None
    try:
        v = [int(x) for x in (proc / "stat").read_text().split("\n")[0].split()[1:9]]
        total = (sum(v), v[3] + v[4])
    except (OSError, ValueError, IndexError):
        pass
    pids = {}
    try:
        carpetas = [d for d in proc.iterdir() if d.name.isdigit()]
    except OSError:
        carpetas = []
    for d in carpetas:
        try:
            if uid is not None and d.stat().st_uid != uid:
                continue
            s = (d / "stat").read_text()
            comm = s[s.index("(") + 1:s.rindex(")")]
            f = s[s.rindex(")") + 1:].split()
            try:
                cmd = (d / "cmdline").read_bytes().decode("utf-8", "replace")
            except OSError:
                cmd = ""
            pids[int(d.name)] = (_nombre_proc(comm, cmd), int(f[11]) + int(f[12]), int(f[21]) * pagina)
        except (OSError, ValueError, IndexError):
            continue  # el proceso murió o no se deja leer
    return time.monotonic(), total, pids


def _calcular(a, b, nucleos, clk):
    """Con dos muestras: (cpu_total_pct o None, procesos_pct, lista de procesos agrupada por nombre)."""
    dt = b[0] - a[0]
    cap = dt * clk * (nucleos or 1)
    total_pct = None
    if a[1] and b[1] and b[1][0] > a[1][0]:
        total_pct = round(100 * (1 - (b[1][1] - a[1][1]) / (b[1][0] - a[1][0])), 1)
    grupos = {}
    for pid, (nom, ticks, rss) in b[2].items():
        d = max(ticks - a[2][pid][1], 0) if pid in a[2] else 0  # pid nuevo: 0 esta vuelta
        g = grupos.setdefault(nom, [0, 0])
        g[0] += d
        g[1] += rss
    pct = lambda t: round(100 * t / cap, 1) if cap > 0 else None
    lista = sorted(({"nombre": n, "cpu_pct": pct(t), "ram_mb": round(r / 1048576, 1)} for n, (t, r) in grupos.items()),
                   key=lambda x: (x["cpu_pct"] or 0, x["ram_mb"]), reverse=True)[:8]
    return total_pct, pct(sum(g[0] for g in grupos.values())), lista


def _ram(proc="/proc"):
    try:
        m = {}
        for l in (Path(proc) / "meminfo").read_text().splitlines():
            k, _, v = l.partition(":")
            m[k] = int(v.split()[0])
        return {"total": m["MemTotal"] // 1024, "disponible": m["MemAvailable"] // 1024}
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _mhz(sys_, archivo):
    vs = []
    try:
        for p in Path(sys_, "devices/system/cpu").glob("cpu[0-9]*/cpufreq/" + archivo):
            try:
                vs.append(int(p.read_text()) // 1000)
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return sum(vs) // len(vs) if vs else None


def recursos(proc="/proc", sys_="/sys", uid=None, ahora=None):
    """RAM, CPU y procesos del equipo para /salud. Sin lectura = null; nunca levanta. CPU por delta contra la lectura anterior (1 s a 10 min) o dos lecturas a 0,5 s."""
    nucleos = os.cpu_count()
    vacio = {"ram_mb": None, "cpu": {"nucleos": nucleos, "mhz_actual": None, "mhz_max": None, "total_pct": None, "procesos_pct": None}, "procesos": []}
    try:
        if uid is None and hasattr(os, "getuid"):
            uid = os.getuid()
        with _RC["lk"]:
            t = time.monotonic()
            if ahora is None and _RC["cache"][1] is not None and t - _RC["cache"][0] < 5:
                return _RC["cache"][1]
            prev, cur = _RC["prev"], _muestra(proc, uid)
            if not (prev and 1 <= cur[0] - prev[0] <= 600):
                time.sleep(0.5)
                prev, cur = cur, _muestra(proc, uid)
            _RC["prev"] = cur
            tot, pp, lista = _calcular(prev, cur, nucleos, _sc("SC_CLK_TCK", 100))
            out = {"ram_mb": _ram(proc), "cpu": {"nucleos": nucleos, "mhz_actual": _mhz(sys_, "scaling_cur_freq"),
                   "mhz_max": _mhz(sys_, "cpuinfo_max_freq"), "total_pct": tot, "procesos_pct": pp if cur[2] else None},
                   "procesos": lista}
            _RC["cache"] = (t, out)
            return out
    except Exception:  # nunca un 500 por medir
        return vacio


_RED = {"lk": threading.Lock(), "tipo": None, "desde": None}


def red_tipo(ip_wlan, ip_salida, salida_ok=True):
    """Pura. wifi: sale por wlan0; movil: sale por otra interfaz; sin_red: el connect UDP falló; None: no se sabe."""
    if not salida_ok:
        return "sin_red"
    if not ip_salida:
        return None
    return "wifi" if ip_wlan and ip_salida == ip_wlan else "movil"


def _leer_red():
    """(ip_wlan0 o None, ip_salida o None, salida_ok) del sistema; sin fcntl (Windows) = (None, None, True) y el tipo queda None. Las IPs no salen de acá hacia el JSON."""
    try:
        import fcntl, socket, struct
    except ImportError:
        return None, None, True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try:
            try:
                ip_wlan = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", b"wlan0"))[20:24])
            except OSError:
                ip_wlan = None  # wlan0 sin IP o sin la interfaz
            try:
                s.connect(("1.1.1.1", 53))  # UDP: no manda nada, solo elige la ruta
            except OSError:
                return ip_wlan, None, False
            return ip_wlan, s.getsockname()[0], True
        finally:
            s.close()
    except Exception:
        return None, None, True


def red_actualizar(leer=None, ahora=None):
    """Mide la red y, si el tipo cambió respecto de lo que vio el proceso, anota `desde`. Devuelve {tipo, desde}; nunca levanta ni expone IPs."""
    try:
        tipo = red_tipo(*(leer or _leer_red)())
    except Exception:
        tipo = None
    with _RED["lk"]:
        if _RED["desde"] is None or tipo != _RED["tipo"]:
            _RED["tipo"], _RED["desde"] = tipo, int(time.time() if ahora is None else ahora)
        return {"tipo": _RED["tipo"], "desde": _RED["desde"]}



# ---------- tráfico de LUBRICANTES, desde su log (http.server escribe una línea por pedido) ----------
# ponytail: el log no trae bytes ni la IP real (todo entra por el túnel = 127.0.0.1): mb e ips quedan null.
RX_PEDIDO = re.compile(r'\[(\d{2}/\w{3}/\d{4} \d{2}:\d{2}:\d{2})\] "[A-Z]+ [^"]*" (\d{3}) ')


def trafico_24h(ruta=LUB_LOG, ahora=None, tope=4 << 20):
    """{pedidos, e4xx, e5xx, mb, ips, por_hora[24]} de las líneas de pedido de las últimas 24 h (hora local del equipo, como las escribe http.server)."""
    ahora = time.time() if ahora is None else ahora
    h = int(ahora // 3600)
    por = [0] * 24
    e4 = e5 = 0
    try:
        with open(ruta, "rb") as f:
            f.seek(max(f.seek(0, 2) - tope, 0))
            texto = f.read().decode("utf-8", "replace")
    except OSError:
        texto = ""
    for m in RX_PEDIDO.finditer(texto):
        try:
            t = time.mktime(time.strptime(m.group(1), "%d/%b/%Y %H:%M:%S"))
        except ValueError:
            continue
        i = int(t // 3600) - (h - 23)
        if 0 <= i < 24 and t <= ahora:
            por[i] += 1
            c = int(m.group(2))
            e4 += 400 <= c < 500
            e5 += c >= 500
    return {"pedidos": sum(por), "e4xx": e4, "e5xx": e5, "mb": None, "ips": None, "por_hora": por}


def sv_arriba(servicio, correr=None):
    """Segundos que lleva arriba un servicio de runit (`sv status`: "run: x: (pid 1) 123s"), o None."""
    try:
        p = (correr or (lambda: subprocess.run(["sv", "status", servicio], capture_output=True, text=True, timeout=5).stdout))()
        m = re.match(r"run: [^:]+: \(pid \d+\) (\d+)s", p)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def equipo():
    try:
        s = os.statvfs(HOME)
        disco = {"total": round(s.f_blocks * s.f_frsize / 1e9, 1), "libre": round(s.f_bavail * s.f_frsize / 1e9, 1)}
    except (OSError, AttributeError):
        disco = None
    try:
        up = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        up = None
    out = HOME / "clipper" / "outbox"
    clipper = None
    if out.is_dir():
        fs = [f.stat() for f in out.iterdir() if f.is_file()]
        clipper = {"clips": len(fs), "mb": round(sum(f.st_size for f in fs) / 1048576, 1),
                   "ultimo": int(max((f.st_mtime for f in fs), default=0)) or None}
    return {"bateria": bateria(), "disco_gb": disco, "server_arriba_s": sv_arriba("lub-server"),
            "equipo_arriba_s": up, "trafico_24h": trafico_24h(), "clipper": clipper, "red": red_actualizar(), **recursos()}


# ---------- vigía: redeploy de LUBRICANTES y de este mismo archivo ----------
def cambio(ruta, mtime0):
    """True si `ruta` cambió desde mtime0, el cambio tiene más de 30 s (el pull ya terminó) y el archivo nuevo compila."""
    try:
        m = ruta.stat().st_mtime
        if m == mtime0 or time.time() - m < 30:
            return False
        compile(ruta.read_bytes(), str(ruta), "exec")
        return True
    except (OSError, SyntaxError, ValueError):
        return False


def mtime(ruta):
    try:
        return ruta.stat().st_mtime
    except OSError:
        return 0.0


def vigilar(minutos):
    lub, yo = LUB / "server.py", ARCHIVO
    m_lub, m_yo = mtime(lub), mtime(yo)
    while True:
        time.sleep(minutos * 60)
        red_actualizar()  # que `desde` no dependa de que alguien pida /salud
        if EN_MAIN and cambio(lub, m_lub):
            r = subprocess.run(["sv", "restart", "lub-server"], capture_output=True, text=True, timeout=60)
            print(f"{time.strftime('%F %T')} server.py de LUBRICANTES cambió: sv restart lub-server rc={r.returncode}", flush=True)
            m_lub = mtime(lub)
        if EN_MAIN and cambio(yo, m_yo):
            print(f"{time.strftime('%F %T')} celu.py cambió: salgo para que runit me levante con el código nuevo", flush=True)
            os._exit(0)


class Handler(BaseHTTPRequestHandler):
    def enviar(self, codigo, cuerpo, tipo):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/salud":
            return self.enviar(200, json.dumps({"ok": True, "equipo": equipo()}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if p == "/robots.txt":
            return self.enviar(200, b"User-agent: *\nDisallow: /\n", "text/plain; charset=utf-8")
        self.enviar(404, b'{"error": "solo /salud"}', "application/json; charset=utf-8")

    def log_message(self, *a):
        pass  # sin log de pedidos: no se guarda quién pregunta


def demo():
    # red: función pura, cambio de tipo (con `desde`), sin IPs en el JSON
    assert [red_tipo(*a) for a in (("192.0.2.5", "192.0.2.5"), ("192.0.2.5", "198.51.100.9"), (None, "198.51.100.9"), ("192.0.2.5", None, False), (None, None))] == ["wifi", "movil", "movil", "sin_red", None]
    g = (_RED["tipo"], _RED["desde"])
    _RED["tipo"] = _RED["desde"] = None
    assert red_actualizar(lambda: ("192.0.2.5", "192.0.2.5", True), 100) == {"tipo": "wifi", "desde": 100}
    assert red_actualizar(lambda: ("192.0.2.5", "192.0.2.5", True), 200)["desde"] == 100
    rj = red_actualizar(lambda: (None, "198.51.100.9", True), 300)
    assert rj == {"tipo": "movil", "desde": 300} and "198.51.100" not in json.dumps(rj)
    assert red_actualizar(lambda: 1 / 0, 400) == {"tipo": None, "desde": 400}
    _RED["tipo"], _RED["desde"] = g
    # /proc y /sys falsos: RAM, nombres cortos, agrupado, % por delta, filtro por uid, sin rutas ni argumentos
    r_ = Path(tempfile.mkdtemp())
    (r_ / "proc").mkdir()
    (r_ / "proc" / "meminfo").write_text("MemTotal:        3145728 kB\nMemFree: 1 kB\nMemAvailable:    1048576 kB\n")
    (r_ / "proc" / "stat").write_text("cpu  100 0 100 700 100 0 0 0\n")

    def pid_(n, comm, cmd, ticks, rss):
        (r_ / "proc" / str(n)).mkdir(exist_ok=True)
        (r_ / "proc" / str(n) / "stat").write_text(f"{n} ({comm}) S " + " ".join(["0"] * 10) + f" {ticks} 0 " + " ".join(["0"] * 8) + f" {rss} 0\n")
        (r_ / "proc" / str(n) / "cmdline").write_bytes(cmd.encode().replace(b" ", b"\0"))
    pid_(10, "python3", "/data/usr/bin/python3 /home/u/lub/server.py --token=SECRETO", 0, 25600)
    pid_(11, "cloudflared", "cloudflared tunnel run --token SECRETO", 0, 2560)
    pid_(12, "runsv", "runsv a", 0, 100)
    pid_(13, "runsv", "runsv b", 0, 100)
    for k, f in enumerate((1000000, 2000000)):
        dd = r_ / "sys" / "devices" / "system" / "cpu" / f"cpu{k}" / "cpufreq"
        dd.mkdir(parents=True)
        (dd / "scaling_cur_freq").write_text(str(f))
        (dd / "cpuinfo_max_freq").write_text("2000000")
    u = (r_ / "proc" / "10").stat().st_uid
    assert _ram(r_ / "proc") == {"total": 3072, "disponible": 1024} and _ram(r_ / "nada") is None
    assert _mhz(r_ / "sys", "scaling_cur_freq") == 1500 and _mhz(r_ / "sys", "cpuinfo_max_freq") == 2000
    ma = _muestra(r_ / "proc", u, 4096)
    assert len(ma[2]) == 4 and not _muestra(r_ / "proc", u + 1, 4096)[2]
    pid_(10, "python3", "python3 /h/server.py", 300, 25600)
    pid_(11, "cloudflared", "x", 100, 2560)
    pid_(12, "runsv", "x", 50, 100)
    pid_(13, "runsv", "x", 50, 100)
    (r_ / "proc" / "stat").write_text("cpu  200 0 200 800 200 0 0 0\n")
    mb = list(_muestra(r_ / "proc", u, 4096))
    mb[0] = ma[0] + 10
    tot, pp, lis = _calcular(ma, tuple(mb), 4, 100)
    assert tot == 50.0 and pp == 12.5, (tot, pp)
    assert [p["nombre"] for p in lis] == ["server.py", "cloudflared", "runsv"] and lis[0] == {"nombre": "server.py", "cpu_pct": 7.5, "ram_mb": 100.0}
    assert "/home" not in json.dumps(lis) and "SECRETO" not in json.dumps(lis)
    # tráfico desde el log: solo líneas de pedido de las últimas 24 h; mb/ips null
    ahora = time.mktime(time.strptime("22/Nov/2026 12:30:00", "%d/%b/%Y %H:%M:%S"))
    lg = r_ / "server.log"
    lg.write_text('127.0.0.1 - - [22/Nov/2026 12:10:00] "GET /salud HTTP/1.1" 200 -\n'
                  '127.0.0.1 - - [22/Nov/2026 11:59:59] "GET /api/x HTTP/1.1" 404 -\n'
                  '127.0.0.1 - - [22/Nov/2026 11:00:00] "POST /api/cargar HTTP/1.1" 500 -\n'
                  'datos recargados (abc): 10 observaciones\n'
                  '127.0.0.1 - - [20/Nov/2026 11:00:00] "GET /viejo HTTP/1.1" 200 -\n', encoding="utf-8")
    t = trafico_24h(lg, ahora)
    assert (t["pedidos"], t["e4xx"], t["e5xx"], t["mb"], t["ips"]) == (3, 1, 1, None, None), t
    assert t["por_hora"][-1] == 1 and t["por_hora"][-2] == 2 and len(t["por_hora"]) == 24
    assert trafico_24h(r_ / "no.log", ahora)["pedidos"] == 0 and "127.0.0.1" not in json.dumps(t)
    # sv status
    assert sv_arriba("x", lambda: "run: lub-server: (pid 17857) 53923s; run: log: (pid 1) 9s\n") == 53923 and sv_arriba("x", lambda: "down: x: 3s\n") is None
    # vigía: cambio viejo y compilable dispara; roto o reciente (pull a medias) no
    f = r_ / "s.py"
    f.write_text("x = 1\n")
    os.utime(f, (time.time() - 120, time.time() - 120))
    assert cambio(f, 0) and not cambio(f, f.stat().st_mtime)
    f.write_text("x = (\n")
    os.utime(f, (time.time() - 120, time.time() - 120))
    assert not cambio(f, 0)
    f.write_text("x = 2\n")
    assert not cambio(f, 0), "menos de 30 s: el pull puede estar a medias"
    # equipo: contrato
    eq = equipo()
    assert set(eq) == {"bateria", "disco_gb", "server_arriba_s", "equipo_arriba_s", "trafico_24h", "clipper", "red", "ram_mb", "cpu", "procesos"}
    assert set(eq["cpu"]) == {"nucleos", "mhz_actual", "mhz_max", "total_pct", "procesos_pct"}
    print("OK")


def main():
    global EN_MAIN
    EN_MAIN = True
    red_actualizar()
    minutos = float(os.environ.get("CELU_VIGIA_MIN", "1"))
    if minutos > 0:
        threading.Thread(target=vigilar, args=(minutos,), daemon=True).start()
    puerto = int(os.environ.get("CELU_PUERTO", "8092"))
    print(f"{time.strftime('%F %T')} celu-servidor en 127.0.0.1:{puerto}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", puerto), Handler).serve_forever()


if __name__ == "__main__":
    demo() if sys.argv[1:] == ["test"] else main()
