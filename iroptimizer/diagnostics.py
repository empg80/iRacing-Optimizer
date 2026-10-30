"""Diagnóstico de problemas a partir de la telemetría grabada (SDK de iRacing + sistema).

Revisa por áreas: CPU, GPU, frametime (tirones), red, memoria, disco y configuración del sistema.
Cada comprobación devuelve estado (ok / aviso / problema / info), la métrica que lo justifica,
una explicación y las acciones concretas para solucionarlo. Además lista los incidentes con su
minuto y su posición en la vuelta.

Los umbrales son heurísticos y están pensados para ser conservadores: mejor un "ok" dudoso que
alarmar por ruido. FrameRate del SDK es una media de 1 s, así que un tirón de 50 ms aparece
suavizado; aun así una caída brusca y breve de la media es una buena pista.
"""
from __future__ import annotations

from collections import Counter
from statistics import median

from .analyzer import _valid, pct
from .config_reader import flat_from_snapshot

OK, WARN, BAD, INFO = "ok", "aviso", "problema", "info"
STATUS_ORDER = {BAD: 0, WARN: 1, INFO: 2, OK: 3}

KNOWN_BG = {  # procesos habituales y qué hacer con ellos
    "obs64.exe": "OBS: usa codificador NVENC (GPU) en vez de x264 (CPU) y baja el preset.",
    "simhub.exe": "SimHub: desactiva plugins/dashboards que no uses y baja su tasa de refresco.",
    "simhubwpf.exe": "SimHub: desactiva plugins/dashboards que no uses y baja su tasa de refresco.",
    "chrome.exe": "Navegador abierto: ciérralo o desactiva la aceleración de vídeo de pestañas en segundo plano.",
    "msedge.exe": "Navegador abierto: ciérralo mientras corres.",
    "firefox.exe": "Navegador abierto: ciérralo mientras corres.",
    "discord.exe": "Discord: desactiva la superposición (overlay) y la aceleración por hardware.",
    "msmpeng.exe": "Antivirus de Windows escaneando: añade una exclusión para la carpeta de iRacing y Documentos\\iRacing.",
    "searchindexer.exe": "Indexador de Windows: excluye las carpetas de iRacing de la indexación.",
    "steam.exe": "Steam: cierra la overlay y las descargas en segundo plano.",
    "steamwebhelper.exe": "Steam: cierra la overlay y las descargas en segundo plano.",
    "icue.exe": "iCUE (RGB): conocido por consumir CPU; ciérralo o reduce los efectos.",
    "armourycrate.service.exe": "Armoury Crate: consume CPU en segundo plano; desactiva lo que no uses.",
    "lghub.exe": "G HUB: cierra la ventana (deja solo el agente).",
    "nvcontainer.exe": "NVIDIA App/overlay: desactiva la superposición y la grabación instantánea si no la usas.",
    "crewchiefv4.exe": "CrewChief: normal que use algo de CPU; si va justo, desactiva funciones que no uses.",
}

THROTTLE_THERMAL = 0x20 | 0x40 | 0x08  # Sw/Hw thermal slowdown, HwSlowdown
THROTTLE_POWER = 0x04 | 0x80           # SwPowerCap, HwPowerBrake


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Check:
    def __init__(self, area, title, status, metric, detail, actions=None):
        self.d = {"area": area, "title": title, "status": status, "metric": metric,
                  "detail": detail, "actions": actions or []}


def _events(rows, cond, kind, describe, min_gap_s=10.0):
    """Agrupa muestras consecutivas que cumplen `cond` en incidentes."""
    out, cur = [], []
    for r in rows:
        if cond(r):
            if cur and r["t"] - cur[-1]["t"] > min_gap_s:
                out.append(cur)
                cur = []
            cur.append(r)
        elif cur and r["t"] - cur[-1]["t"] > min_gap_s:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"kind": kind, "session_time": ev[0].get("session_time"), "session_type": ev[0].get("session_type"),
             "lap_pct": ev[0].get("lap_pct"), "duration_s": round(ev[-1]["t"] - ev[0]["t"] + 0.25, 1),
             "detail": describe(ev)} for ev in out]


def _hitches(rows, hz):
    """Caídas bruscas y breves de FPS respecto a la mediana de los 5 s anteriores."""
    win = max(8, int(5 * hz))
    events = []
    i = win
    while i < len(rows) - 1:
        base = median(r["fps"] for r in rows[i - win:i])
        r = rows[i]
        if base > 30 and r["fps"] < 0.7 * base and r["t"] - rows[i - 1]["t"] < 2:
            j = i
            while j < len(rows) - 1 and rows[j]["fps"] < 0.85 * base and j - i < int(4 * hz):
                j += 1
            recovered = rows[j]["fps"] >= 0.85 * base
            if recovered:  # breve: es un tirón, no una caída sostenida
                seg = rows[i:j + 1]
                prev = rows[i - win:i]
                cause = _hitch_cause(seg, prev)
                events.append({"kind": "tirón", "session_time": r.get("session_time"),
                               "session_type": r.get("session_type"), "lap_pct": r.get("lap_pct"),
                               "duration_s": round(seg[-1]["t"] - seg[0]["t"] + 1 / hz, 1),
                               "detail": f"{base:.0f} → {min(x['fps'] for x in seg):.0f} fps · causa probable: {cause}",
                               "cause": cause})
                i = j + 1
                continue
        i += 1
    return events


def _hitch_cause(seg, prev):
    def mx(rs, k):
        v = [x.get(k) for x in rs if x.get(k) is not None]
        return max(v) if v else None

    def av(rs, k):
        v = [x.get(k) for x in rs if x.get(k) is not None]
        return sum(v) / len(v) if v else None
    if any(x.get("on_pit_road") for x in seg):
        return "boxes"
    pf = mx(seg, "page_faults")
    if pf and pf > 50:
        return "memoria/paginación"
    dr, drp = mx(seg, "disk_read_mbs"), av(prev, "disk_read_mbs")
    if dr and dr > 40 and dr > 3 * (drp or 0.1):
        return "carga de texturas desde disco"
    cn, cnp = mx(seg, "cars_near"), av(prev, "cars_near")
    if cn is not None and cnp is not None and cn - cnp >= 3:
        return "llegada de tráfico"
    bg, bgp = mx(seg, "cpu_bg"), av(prev, "cpu_bg")
    if bg and bgp and bg > 70 and bg > bgp * 1.4:
        return "pico del hilo de física/red (BG)"
    lat = mx(seg, "chan_latency")
    if lat and lat > 0.15:
        return "pico de red"
    g = mx(seg, "gpu_util")
    if g and g >= 97:
        return "GPU"
    sc = av(seg, "sys_cpu_avg")
    scp = av(prev, "sys_cpu_avg")
    if sc and scp and sc > scp + 15:
        return "proceso en segundo plano"
    return "sin causa clara (shader/carga puntual)"


def run(rows: list[dict], meta: dict, analysis: dict) -> dict:
    """`rows` debe haber pasado ya por analyzer.analyze (porcentajes normalizados)."""
    hz = float(meta.get("hz") or 4)
    valid = _valid(rows)
    a = analysis
    g = a["groups"][a["primary"]]
    target = a["target_fps"]
    hw = meta.get("hardware") or {}
    cfg_r = flat_from_snapshot(((meta.get("config") or {}).get("renderer")) or {})
    cfg_app = flat_from_snapshot(((meta.get("config") or {}).get("app")) or {})
    dur_h = max(1e-6, sum(gr["duration_min"] for gr in a["groups"].values()) / 60)
    checks: list[dict] = []
    events: list[dict] = []

    def add(*args, **kw):
        checks.append(Check(*args, **kw).d)

    # ------------------------------------------------------------------ CPU
    bn = g["bottleneck"]
    cpu_share = bn["cpu"] + bn["mixed"] / 2
    below = g["below_target_pct"]
    st = BAD if cpu_share >= 60 and below > 30 else WARN if cpu_share >= 40 and below > 15 else OK
    pmb = (a.get("presentmon") or {}).get("bound_pct")
    add("CPU", "Límite de CPU / motor", st,
        f"{cpu_share:.0f} % del tiempo limitado por CPU · {below:.0f} % bajo objetivo"
        + (f" · PresentMon: CPU {pmb['cpu']:.0f} % / GPU {pmb['gpu']:.0f} % de los frames" if pmb else ""),
        "La GPU está esperando frames: el hilo principal de iRacing (lógica + preparar el dibujo) marca el ritmo. "
        "Es lo normal en iRacing con parrillas grandes; se combate reduciendo lo que hay que dibujar.",
        ["Aplica las recomendaciones de CPU de la pestaña Análisis (coches dibujados, espejos, objetos de boxes, gradas, LOD dinámico).",
         "Cierra programas en segundo plano (ver comprobación de carga de fondo)."] if st != OK else [])

    bg = g.get("cpu_bg") or {}
    bgp95 = bg.get("p95")
    if bgp95 is not None:
        st = BAD if bgp95 >= 85 else WARN if bgp95 >= 65 else OK
        add("CPU", "Hilo de física y red (BG)", st, f"CpuUsageBG p95 {bgp95:.0f} % · máx. {bg.get('max', 0):.0f} %",
            "El hilo BG calcula la física y la red. Si se satura aparecen tirones en el force feedback, "
            "saltos de coches (warping) y caídas aunque la GPU y el hilo principal vayan sobrados.",
            ["Asegúrate de que nada fija la afinidad de iRacing a pocos núcleos (Process Lasso, etc.).",
             "Cierra software que use mucha CPU en segundo plano.",
             "Reduce la frecuencia del FFB a 60 Hz solo si los tirones coinciden con este indicador."] if st != OK else [])
        events += _events(valid, lambda r: (r.get("cpu_bg") or 0) >= 85, "hilo BG saturado",
                          lambda ev: f"CpuUsageBG hasta {max(x['cpu_bg'] for x in ev):.0f} %")

    core = [r.get("sys_cpu_max_core") for r in valid if r.get("sys_cpu_max_core") is not None]
    if core:
        sat = 100 * sum(1 for c in core if c >= 98) / len(core)
        st = WARN if sat >= 15 else OK
        add("CPU", "Núcleo saturado", st, f"Algún núcleo al ≥ 98 % el {sat:.0f} % del tiempo",
            "Un núcleo al 100 % casi continuo indica que el hilo principal no tiene margen; cualquier proceso que "
            "comparta ese núcleo provoca tirones.",
            ["En CPUs con varios CCD (7950X3D, 9950X3D) asegúrate de que iRacing corre en el CCD con 3D V-Cache (Game Bar / driver de chipset AMD).",
             "Cierra programas en segundo plano."] if st != OK else [])

    sca = (g.get("sys_cpu_avg") or {}).get("median")
    ir_cpu = meta.get("iracing_cpu_pct")
    tops = [p for p in (meta.get("top_processes") or []) if not p.get("is_iracing")]
    if sca is not None:
        other = sca - ir_cpu if ir_cpu is not None else None
        ref = other if other is not None else sca
        lim = (20, 35) if other is not None else (35, 55)
        st = BAD if ref >= lim[1] else WARN if ref >= lim[0] else OK
        metric = f"CPU total {sca:.0f} %" + (f" · iRacing {ir_cpu:.0f} % · resto {other:.0f} %" if other is not None else "")
        acts = []
        for p in tops[:6]:
            tip = KNOWN_BG.get(p["name"].lower())
            acts.append(f"{p['name']} ({p['cpu_pct']:.1f} %)" + (f": {tip}" if tip else ""))
        if st != OK and not tops:
            acts.append("Graba con la versión 0.3.1+ para ver qué procesos consumen CPU, o mira el Administrador de tareas con el coche en pista.")
        add("CPU", "Carga de CPU en segundo plano", st, metric,
            "Todo lo que no es iRacing compite por la CPU y, en los X3D, por la caché. Con iRacing limitado por CPU, "
            "cada punto cuenta.", acts if st != OK else acts[:3])

    # ------------------------------------------------------------------ GPU
    gp = g.get("gpu") or {}
    gp95 = gp.get("p95")
    if gp95 is not None:
        if gp95 >= 97 and below > 15:
            st, txt = BAD, "La GPU es el límite: baja ajustes que cuestan GPU (MSAA, sombras, shaders, espejos en alto) o la resolución."
        elif gp95 < 70 and cpu_share > 50:
            st, txt = INFO, "La GPU va sobrada: puedes subir calidad gráfica (MSAA, sombras) sin perder FPS."
        else:
            st, txt = OK, "Carga de GPU normal."
        add("GPU", "Carga de GPU", st, f"Uso p95 {gp95:.0f} %", txt)
    temps = [r.get("gpu_temp") for r in valid if r.get("gpu_temp") is not None]
    thr = [int(r["gpu_throttle"]) for r in valid if r.get("gpu_throttle") is not None]
    if temps:
        tmax = max(temps)
        therm = 100 * sum(1 for x in thr if x & THROTTLE_THERMAL) / len(thr) if thr else 0
        st = BAD if tmax >= 87 or therm >= 5 else WARN if tmax >= 83 or therm > 0.5 else OK
        add("GPU", "Temperatura y limitación térmica", st,
            f"Máx. {tmax:.0f} °C" + (f" · limitación térmica {therm:.1f} % del tiempo" if thr else ""),
            "Si la GPU se calienta baja frecuencias y pierdes rendimiento de forma irregular.",
            ["Revisa la curva de ventiladores y el flujo de aire de la caja.",
             "Limpia el polvo de los disipadores.", "Un undervolt ligero suele bajar 5-10 °C sin perder rendimiento."] if st != OK else [])
        events += _events(valid, lambda r: (r.get("gpu_temp") or 0) >= 85 or int(r.get("gpu_throttle") or 0) & THROTTLE_THERMAL,
                          "GPU caliente/limitada", lambda ev: f"hasta {max(x.get('gpu_temp') or 0 for x in ev):.0f} °C")
    mem = a["memory"]
    vt, vu, vc = mem.get("vram_total_mb"), mem.get("vram_used_max_mb"), mem.get("vidmem_cfg_mb")
    if vt and vu:
        ratio = vu / vt
        st = BAD if ratio >= 0.97 else WARN if ratio >= 0.9 or (vc and vu > vc * 0.98) else OK
        add("GPU", "Memoria de vídeo (VRAM)", st,
            f"Usada máx. {vu / 1024:.1f} de {vt / 1024:.0f} GB" + (f" · asignada a iRacing {vc / 1024:.0f} GB" if vc else ""),
            "Si la VRAM se llena, las texturas se recargan durante la carrera y aparecen tirones y texturas borrosas.",
            ["Sube VidMemToUseMB/VidMemMB hasta ~85-90 % de tu VRAM.",
             "Cierra apps que usan GPU (navegador con vídeo, overlays con aceleración).",
             "Baja calidad de texturas o desactiva CarPaint2048x2048."] if st != OK else [])

    # ------------------------------------------------------------- frametime
    pm = a.get("presentmon") or {}
    if pm:
        spm = pm["stutters_per_min"]
        ratio = pm["low1_fps"] / pm["avg_fps"] if pm.get("avg_fps") else 1
        st = BAD if spm >= 3 or ratio < 0.5 else WARN if spm >= 1 or ratio < 0.65 else OK
        pm_causes = Counter(e["cause"] for e in pm.get("stutter_events", []))
        acts = []
        if pm_causes.get("CPU"):
            acts.append("Tirones de CPU: el hilo principal tardó en preparar el frame. Revisa coches dibujados, "
                        "objetos de boxes y procesos en segundo plano.")
        if pm_causes.get("GPU"):
            acts.append("Tirones de GPU: frames puntuales muy pesados (sombras, reflejos, espejos en alto). Baja esos ajustes.")
        if pm_causes.get("espera/sincronización"):
            acts.append("Tirones sin CPU ni GPU ocupadas: carga de texturas/shaders, V-Sync o procesos que bloquean. "
                        "Deja 1-2 vueltas de calentamiento, usa SSD y cierra overlays.")
        add("Frametime", "Tirones reales (PresentMon)", st,
            f"{pm['stutters_over_2x']} frames > 2× la mediana · {spm:.1f}/min · 1 % low {pm['low1_fps']:.0f} "
            f"de {pm['avg_fps']:.0f} fps de media · peor frame {pm['frametime_max_ms']:.0f} ms",
            "Medido frame a frame con PresentMon: es la medida más fiable de la suavidad. Un 1 % low muy por "
            "debajo de la media se nota como tirones aunque los FPS medios sean altos.", acts)
        for ev in pm.get("stutter_events", []):
            events.append({"kind": f"tirón ({ev['cause']})", "session_time": ev.get("session_time"),
                           "session_type": ev.get("session_type"), "lap_pct": ev.get("lap_pct"),
                           "duration_s": round(((ev.get("t_end") or 0) - (ev.get("t") or 0)), 2),
                           "detail": f"frame de {ev['max_ft']:.0f} ms (normal {ev['base']:.1f} ms) · {ev['frames']} frames"
                                     + (f" · {int(ev['cars_near'])} coches cerca" if ev.get("cars_near") is not None else "")})
    rows_sorted = sorted(valid, key=lambda r: r["t"])
    hitches = _hitches(rows_sorted, hz)
    if not pm:
        events += hitches
    rate = len(hitches) / dur_h
    st = BAD if rate >= 20 else WARN if rate >= 6 else OK
    causes = Counter(h["cause"] for h in hitches)
    acts = []
    if causes.get("carga de texturas desde disco"):
        acts.append("Instala iRacing en un SSD NVMe y deja 1-2 vueltas de calentamiento: la primera vuelta carga texturas.")
    if causes.get("llegada de tráfico"):
        acts.append("Tirones al alcanzar coches: baja MaxCarsToDraw y coches en espejos; sube LODMinFPSTarget.")
    if causes.get("proceso en segundo plano"):
        acts.append("Un proceso en segundo plano se activó durante los tirones: revisa la lista de carga de fondo.")
    if causes.get("pico del hilo de física/red (BG)") or causes.get("pico de red"):
        acts.append("Tirones de física/red: usa cable, cierra descargas y revisa la comprobación de red.")
    if causes.get("sin causa clara (shader/carga puntual)"):
        acts.append("Tirones sin causa visible: suele ser compilación de shaders o carga de un coche/objeto nuevo. "
                    "Si persisten tras 2 vueltas, actualiza el driver de GPU y mide con PresentMon.")
    if causes.get("boxes"):
        acts.append("Tirones en boxes: baja MaxPitObjsToDraw y PitObjectDetail.")
    if not pm:
        add("Frametime", "Tirones (caídas bruscas y breves)", st,
            f"{len(hitches)} tirones · {rate:.1f}/hora" + (" · " + ", ".join(f"{k}: {v}" for k, v in causes.most_common(3)) if causes else ""),
            "Caída repentina de la media de FPS del SDK (aprox.: el SDK promedia 1 s). Para medirlos frame a frame "
            "activa PresentMon en el monitor.",
            acts if st != OK else acts[:2])

    drops = a.get("drops") or []
    if drops:
        worst = drops[0]
        st = WARN if worst["min_fps"] < target * 0.5 else OK
        add("Frametime", "Caídas sostenidas", st,
            f"{len(drops)} caídas · peor {worst['min_fps']:.0f} fps durante {worst['duration_s']:.0f} s",
            "Periodos largos por debajo del 80 % de tu mediana (salidas, pelotones, zonas pesadas del circuito). "
            "Ver «Peores caídas» y «Zonas más lentas» en el informe.",
            ["Aplica las recomendaciones de la pestaña Análisis."] if st != OK else [])

    # ------------------------------------------------------------------ red
    lat = [r["chan_latency"] * 1000 for r in valid if r.get("chan_latency") is not None]
    if lat:
        med_l, p95_l, max_l = median(lat), pct(lat, 95), max(lat)
        thr_l = max(med_l * 2, med_l + 60)
        spikes = _events(valid, lambda r: r.get("chan_latency") is not None and r["chan_latency"] * 1000 > thr_l,
                         "pico de latencia", lambda ev: f"hasta {max(x['chan_latency'] for x in ev) * 1000:.0f} ms")
        events += spikes
        srate = len(spikes) / dur_h
        st = BAD if p95_l > 200 or srate >= 20 else WARN if p95_l > 130 or srate >= 6 else OK
        add("Red", "Latencia con el servidor", st,
            f"Mediana {med_l:.0f} ms · p95 {p95_l:.0f} ms · máx. {max_l:.0f} ms · {len(spikes)} picos",
            "La latencia depende sobre todo de la distancia al servidor; lo que importa es que sea estable. "
            "Los picos provocan saltos de coches y contactos fantasma.",
            ["Conéctate por cable, no por Wi-Fi.", "Pausa descargas, actualizaciones y sincronizaciones en la nube.",
             "Si emites, limita el bitrate de subida de OBS a ~70 % de tu subida real.",
             "Activa QoS/SQM en el router si lo soporta."] if st != OK else [])
    q = [r.get("chan_quality") for r in valid if r.get("chan_quality") is not None]
    pq = [r.get("chan_partner_quality") for r in valid if r.get("chan_partner_quality") is not None]
    if q:
        qp5, pqp5 = pct(q, 5), pct(pq, 5) if pq else None
        st = BAD if qp5 < 85 else WARN if qp5 < 95 else OK
        if pqp5 is not None and pqp5 < 95 and st == OK:
            st = INFO
        add("Red", "Calidad de conexión (pérdida de paquetes)", st,
            f"Tuya p5 {qp5:.0f} %" + (f" · servidor→tú p5 {pqp5:.0f} %" if pqp5 is not None else ""),
            "ChanQuality baja = paquetes perdidos en tu lado; si solo baja la del servidor (partner), el problema "
            "suele estar fuera de tu casa.",
            ["Cable de red y, si puedes, reinicia el router antes de carreras largas.",
             "Comprueba que no haya otro equipo de la casa saturando la conexión."] if st in (WARN, BAD) else [])
        events += _events(valid, lambda r: (r.get("chan_quality") or 100) < 90, "pérdida de paquetes",
                          lambda ev: f"calidad mín. {min(x['chan_quality'] for x in ev):.0f} %")
    sk = [abs(r["clock_skew"]) for r in valid if r.get("clock_skew") is not None]
    if sk:
        big = 100 * sum(1 for x in sk if x > 0.1) / len(sk)
        st = WARN if big > 1 else OK
        add("Red", "Desfase de reloj con el servidor", st, f"> 0,1 s el {big:.1f} % del tiempo · máx. {max(sk):.2f} s",
            "ChanClockSkew alto indica que tu simulación va desincronizada del servidor (CPU saturada o red inestable).",
            ["Revisa la carga del hilo BG y la estabilidad de la red."] if st != OK else [])

    # ------------------------------------------------------------- memoria
    pf = [r.get("page_faults") for r in valid if r.get("page_faults") is not None]
    ram_t, ram_u = mem.get("ram_total_mb"), mem.get("ram_used_max_mb")
    if pf or ram_t:
        pf95 = pct(pf, 95) if pf else 0
        ratio = (ram_u / ram_t) if ram_t and ram_u else 0
        st = BAD if pf95 > 200 or ratio > 0.95 else WARN if pf95 > 50 or ratio > 0.88 else OK
        add("Memoria", "RAM y paginación", st,
            f"Fallos de página p95 {pf95:.0f}/s · RAM usada máx. {ratio * 100:.0f} %",
            "Si Windows tiene que paginar a disco, aparecen tirones largos y cargas lentas.",
            ["Cierra programas que consumen mucha RAM.", "Sube SysMemToUseMB/MaxWorkingSetMB_64Bit si iRacing se acerca al límite.",
             "Activa el perfil XMP/EXPO de la RAM en la BIOS."] if st != OK else [])

    # ------------------------------------------------------- configuración
    disk_log = (cfg_app.get("irsdkenabledisk") == "1") and (cfg_app.get("irsdkautologdisk") == "1")
    logging_rows = sum(1 for r in valid if r.get("disk_logging"))
    if disk_log or logging_rows:
        add("Sistema", "Telemetría grabándose en disco (.ibt)", INFO,
            "irsdkAutoLogDisk=1" + (f" · activo el {100 * logging_rows / len(valid):.0f} % de la sesión" if logging_rows else ""),
            "iRacing está guardando un archivo .ibt de telemetría continuamente"
            + (" de todos los coches (irsdkLogAllCars=1)" if cfg_app.get("irsdklogallcars") == "1" else "")
            + ". El coste es pequeño, pero escribe en disco sin parar y llena la carpeta de telemetría.",
            ["Si no analizas telemetría (Garage61, VRS, MoTeC…), pon irsdkAutoLogDisk=0 en app.ini; puedes activarla a mano con Alt+L.",
             "Si la usas, pon irsdkLogAllCars=0 para grabar solo tu coche."])

    win = hw.get("windows") or {}
    pp = (win.get("power_plan") or {}).get("name")
    if pp:
        st = WARN if pp == "Economizador" else OK
        add("Sistema", "Plan de energía", st, pp,
            "Con 'Economizador' la CPU no sube frecuencias.", ["Usa 'Equilibrado' (recomendado por AMD para Ryzen) o 'Alto rendimiento'."] if st != OK else [])
    if win.get("hags") is False:
        add("Sistema", "Programación de GPU acelerada por hardware", INFO, "Desactivada",
            "En algunos equipos mejora la latencia y la estabilidad del frametime.",
            ["Pruébala activada (Configuración → Pantalla → Gráficos) y compara dos sesiones con «Comparar»."])
    vs = cfg_r.get("verticalsync")
    lim = cfg_r.get("limitframerate")
    if vs == "1":
        add("Sistema", "V-Sync de iRacing activado", WARN, "VerticalSync=1",
            "Con G-Sync/FreeSync conviene V-Sync del juego desactivado y tope de FPS unos fps por debajo de la frecuencia.",
            ["Pon VerticalSync=0, activa V-Sync en el panel del driver y usa LimitFrameRate=1 con un tope por debajo de los Hz."])
    elif lim == "0":
        add("Sistema", "Sin tope de FPS", WARN, "LimitFrameRate=0",
            "Sin tope los FPS oscilan mucho y el input lag varía.", ["Activa el tope (ver recomendaciones de Análisis)."])

    checks.sort(key=lambda c: (STATUS_ORDER[c["status"]], c["area"]))
    events.sort(key=lambda e: (e.get("session_type") or "", e.get("session_time") or 0))
    summary = Counter(c["status"] for c in checks)
    return {"checks": checks, "events": events[:200], "summary": dict(summary),
            "hitches_per_hour": rate}


class LiveWatch:
    """Alertas en vivo durante la grabación (para la GUI). Devuelve mensajes nuevos por muestra."""

    def __init__(self, hz=4.0, cooldown_s=30.0):
        self.hz = hz
        self.cool = cooldown_s
        self.last: dict[str, float] = {}
        self.fps_hist: list[float] = []

    def _emit(self, key, t, msg, out):
        if t - self.last.get(key, -1e9) >= self.cool:
            self.last[key] = t
            out.append(msg)

    def feed(self, r: dict) -> list[str]:
        out: list[str] = []
        if not r.get("on_track"):
            return out
        t = r.get("t") or 0
        fps = _f(r.get("fps"))
        if fps:
            if len(self.fps_hist) >= 4:
                base = median(self.fps_hist[-20:])
                if fps < 0.7 * base:
                    self._emit("hitch", t, f"Tirón: {base:.0f} → {fps:.0f} fps (coches cerca: {r.get('cars_near', '?')})", out)
            self.fps_hist.append(fps)
            self.fps_hist = self.fps_hist[-40:]
        lat = _f(r.get("chan_latency"))
        if lat and lat > 0.15:
            self._emit("lat", t, f"Pico de latencia: {lat * 1000:.0f} ms", out)
        q = _f(r.get("chan_quality"))
        if q is not None:
            q = q * 100 if q <= 1.5 else q
            if q < 90:
                self._emit("q", t, f"Pérdida de paquetes: calidad {q:.0f} %", out)
        bg = _f(r.get("cpu_bg"))
        if bg is not None:
            bg = bg * 100 if bg <= 1.5 else bg
            if bg >= 85:
                self._emit("bg", t, f"Hilo de física/red (BG) al {bg:.0f} %", out)
        gt = _f(r.get("gpu_temp"))
        if gt and gt >= 85:
            self._emit("temp", t, f"GPU a {gt:.0f} °C", out)
        thr = r.get("gpu_throttle")
        if thr is not None and int(thr) & THROTTLE_THERMAL:
            self._emit("thr", t, "GPU limitando frecuencia por temperatura", out)
        pf = _f(r.get("page_faults"))
        if pf and pf > 200:
            self._emit("pf", t, f"Paginación a disco: {pf:.0f} fallos/s", out)
        return out
