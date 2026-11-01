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

from .i18n import _

from collections import Counter
from statistics import median

from .analyzer import _valid, pct
from .config_reader import flat_from_snapshot

OK, WARN, BAD, INFO = "ok", "aviso", "problema", "info"
STATUS_ORDER = {BAD: 0, WARN: 1, INFO: 2, OK: 3}

KNOWN_BG = {  # procesos habituales y qué hacer con ellos
    "obs64.exe": _("OBS: usa codificador NVENC (GPU) en vez de x264 (CPU) y baja el preset."),
    "simhub.exe": _("SimHub: desactiva plugins/dashboards que no uses y baja su tasa de refresco."),
    "simhubwpf.exe": _("SimHub: limita los fps del dashboard (30 suele bastar) y desactiva los plugins que no uses."),
    "chrome.exe": _("Navegador abierto: ciérralo o desactiva la aceleración de vídeo de pestañas en segundo plano."),
    "msedge.exe": _("Navegador abierto: ciérralo mientras corres."),
    "firefox.exe": _("Navegador abierto: ciérralo mientras corres."),
    "discord.exe": _("Discord: desactiva su superposición (overlay) en juego. Deja la aceleración por hardware activada si tu GPU va sobrada."),
    "msedgewebview2.exe": _("Interfaz web de otro programa (overlays, Garage 61, widgets…). Graba con la v0.8 o posterior para ver a qué programa pertenece."),
    "obs-browser-page.exe": _("Fuentes de navegador de OBS (alertas, chat, overlays web): ponlas a 30 fps, quita las ocultas y activa «Apagar la fuente cuando no sea visible»."),
    "msmpeng.exe": _("Antivirus de Windows escaneando: añade una exclusión para la carpeta de iRacing y Documentos\\iRacing."),
    "searchindexer.exe": _("Indexador de Windows: excluye las carpetas de iRacing de la indexación."),
    "steam.exe": _("Steam: cierra la overlay y las descargas en segundo plano."),
    "steamwebhelper.exe": _("Steam: cierra la overlay y las descargas en segundo plano."),
    "icue.exe": _("iCUE (RGB): conocido por consumir CPU; ciérralo o reduce los efectos."),
    "armourycrate.service.exe": _("Armoury Crate: consume CPU en segundo plano; desactiva lo que no uses."),
    "lghub.exe": _("G HUB: cierra la ventana (deja solo el agente)."),
    "nvcontainer.exe": _("NVIDIA App/overlay: desactiva la superposición y la grabación instantánea si no la usas."),
    "crewchiefv4.exe": _("CrewChief: normal que use algo de CPU; si va justo, desactiva funciones que no uses."),
}

# Coincidencia por fragmento de nombre (programas de simracing con nombres de ejecutable variables)
KNOWN_BG_PARTS = (
    ("garage61", _("Garage 61: normal que use algo de CPU mientras sube vueltas; no hace falta cerrarlo.")),
    ("simpro", _("SimPro Manager (Simagic): consumo bajo; ciérralo si no cambias ajustes del volante en carrera.")),
    ("ifuel", _("iFuel (overlay): si va justo, baja su frecuencia de refresco.")),
    ("racelab", _("Overlays de RaceLab: desactiva los widgets que no veas y limita su refresco.")),
    ("ioverlay", _("iOverlay: desactiva los widgets que no veas y limita su refresco.")),
    ("crewchief", _("CrewChief: normal que use algo de CPU; si va justo, desactiva funciones que no uses.")),
)
STREAMING = ("obs64.exe", "obs32.exe", "streamlabs obs.exe", "streamlabs desktop.exe", "xsplit.core.exe")


def bg_tip(name: str) -> str | None:
    """Consejo para un proceso; «msedgewebview2.exe (Programa.exe)» se resuelve por el programa que lo abrió."""
    low = name.lower()
    owner = low.partition(" (")[2]
    owner = owner.rstrip(")")
    if owner:
        tip = bg_tip(owner)
        what = _("Interfaz web de {owner}", owner=owner) if owner != "?" else _("Interfaz web de otro programa (WebView2)")
        return what + (": " + tip if tip else "")
    if low in KNOWN_BG:
        return KNOWN_BG[low]
    return next((t for part, t in KNOWN_BG_PARTS if part in low), None)


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
                events.append({"kind": _("tirón"), "session_time": r.get("session_time"),
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
        return _("memoria/paginación")
    dr, drp = mx(seg, "disk_read_mbs"), av(prev, "disk_read_mbs")
    if dr and dr > 40 and dr > 3 * (drp or 0.1):
        return _("carga de texturas desde disco")
    cn, cnp = mx(seg, "cars_near"), av(prev, "cars_near")
    if cn is not None and cnp is not None and cn - cnp >= 3:
        return _("llegada de tráfico")
    bg, bgp = mx(seg, "cpu_bg"), av(prev, "cpu_bg")
    if bg and bgp and bg > 70 and bg > bgp * 1.4:
        return _("pico del hilo de física/red (BG)")
    lat = mx(seg, "chan_latency")
    if lat and lat > 0.15:
        return _("pico de red")
    g = mx(seg, "gpu_util")
    if g and g >= 97:
        return "GPU"
    sc = av(seg, "sys_cpu_avg")
    scp = av(prev, "sys_cpu_avg")
    if sc and scp and sc > scp + 15:
        return _("proceso en segundo plano")
    return _("sin causa clara (shader/carga puntual)")


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
    add("CPU", _("Límite de CPU / motor"), st,
        _("{cpu_share:.0f} % del tiempo limitado por CPU · {below:.0f} % bajo objetivo", cpu_share=cpu_share, below=below)
        + (_(" · PresentMon: CPU {p0:.0f} % / GPU {p1:.0f} % de los frames", p0=pmb['cpu'], p1=pmb['gpu']) if pmb else ""),
        _("La GPU está esperando frames: el hilo principal de iRacing (lógica + preparar el dibujo) marca el ritmo. Es lo normal en iRacing con parrillas grandes; se combate reduciendo lo que hay que dibujar."),
        [_("Aplica las recomendaciones de CPU de la pestaña Análisis (coches dibujados, espejos, objetos de boxes, gradas, LOD dinámico)."),
         _("Cierra programas en segundo plano (ver comprobación de carga de fondo).")] if st != OK else [])

    bg = g.get("cpu_bg") or {}
    bgp95 = bg.get("p95")
    if bgp95 is not None:
        st = BAD if bgp95 >= 85 else WARN if bgp95 >= 65 else OK
        add("CPU", _("Hilo de física y red (BG)"), st, _("CpuUsageBG p95 {bgp95:.0f} % · máx. {p1:.0f} %", bgp95=bgp95, p1=bg.get('max', 0)),
            _("El hilo BG calcula la física y la red. Si se satura aparecen tirones en el force feedback, saltos de coches (warping) y caídas aunque la GPU y el hilo principal vayan sobrados."),
            [_("Asegúrate de que nada fija la afinidad de iRacing a pocos núcleos (Process Lasso, etc.)."),
             _("Cierra software que use mucha CPU en segundo plano."),
             _("Reduce la frecuencia del FFB a 60 Hz solo si los tirones coinciden con este indicador.")] if st != OK else [])
        events += _events(valid, lambda r: (r.get("cpu_bg") or 0) >= 85, "hilo BG saturado",
                          lambda ev: f"CpuUsageBG hasta {max(x['cpu_bg'] for x in ev):.0f} %")

    core = [r.get("sys_cpu_max_core") for r in valid if r.get("sys_cpu_max_core") is not None]
    if core:
        sat = 100 * sum(1 for c in core if c >= 98) / len(core)
        st = WARN if sat >= 15 else OK
        add("CPU", _("Núcleo saturado"), st, _("Algún núcleo al ≥ 98 % el {sat:.0f} % del tiempo", sat=sat),
            _("Un núcleo al 100 % casi continuo indica que el hilo principal no tiene margen; cualquier proceso que comparta ese núcleo provoca tirones."),
            [_("En CPUs con varios CCD (7950X3D, 9950X3D) asegúrate de que iRacing corre en el CCD con 3D V-Cache (Game Bar / driver de chipset AMD)."),
             _("Cierra programas en segundo plano.")] if st != OK else [])

    sca = (g.get("sys_cpu_avg") or {}).get("median")
    ir_cpu = meta.get("iracing_cpu_pct")
    tops = [p for p in (meta.get("top_processes") or []) if not p.get("is_iracing")]
    if sca is not None:
        other = sca - ir_cpu if ir_cpu is not None else None
        ref = other if other is not None else sca
        lim = (20, 35) if other is not None else (35, 55)
        st = BAD if ref >= lim[1] else WARN if ref >= lim[0] else OK
        metric = _("CPU total {sca:.0f} %", sca=sca) + (_(" · iRacing {ir:.0f} % · resto {other:.0f} %", ir=ir_cpu, other=other) if other is not None else "")
        acts = []
        for p in tops[:6]:
            tip = bg_tip(p["name"])
            if p["name"].lower() in STREAMING and p["cpu_pct"] < 5:
                tip = _("Consumo bajo: señal de que ya codificas con la GPU (NVENC). Bien así.")
            acts.append(f"{p['name']} ({p['cpu_pct']:.1f} %)" + (f": {tip}" if tip else ""))
        if st != OK and not tops:
            acts.append(_("Graba con la versión 0.3.1+ para ver qué procesos consumen CPU, o mira el Administrador de tareas con el coche en pista."))
        streaming = any(p["name"].lower() in STREAMING for p in tops)
        if streaming and st in (WARN, BAD):
            # Con OBS emitiendo es el coste esperado del directo: se informa, sin alarmar
            st = WARN if st == BAD else INFO
            detail = _("Estás emitiendo: es el coste de tu directo (OBS, overlays, dashboards). Como tu límite es el hilo principal de iRacing, recortarlo da unos pocos FPS en los peores momentos, no grandes saltos. Empieza por lo más barato de quitar.")
        else:
            detail = _("Todo lo que no es iRacing compite por la CPU y, en los X3D, por la caché. Con iRacing limitado por CPU, cada punto cuenta.")
        add("CPU", _("Carga de CPU en segundo plano") + (_(" (emitiendo)") if streaming else ""), st, metric,
            detail, acts if st != OK else acts[:3])

    # ------------------------------------------------------------------ GPU
    gp = g.get("gpu") or {}
    gp95 = gp.get("p95")
    if gp95 is not None:
        if gp95 >= 97 and below > 15:
            st, txt = BAD, _("La GPU es el límite: baja ajustes que cuestan GPU (MSAA, sombras, shaders, espejos en alto) o la resolución.")
        elif gp95 < 70 and cpu_share > 50:
            st, txt = INFO, _("La GPU va sobrada: el MSAA o la resolución salen casi gratis. Ojo: en iRacing las sombras, los espejos y los reflejos también cargan la CPU; súbelos solo si tienes margen de CPU.")
        else:
            st, txt = OK, _("Carga de GPU normal.")
        add("GPU", _("Carga de GPU"), st, _("Uso p95 {gp95:.0f} %", gp95=gp95), txt)
    temps = [r.get("gpu_temp") for r in valid if r.get("gpu_temp") is not None]
    thr = [int(r["gpu_throttle"]) for r in valid if r.get("gpu_throttle") is not None]
    if temps:
        tmax = max(temps)
        therm = 100 * sum(1 for x in thr if x & THROTTLE_THERMAL) / len(thr) if thr else 0
        st = BAD if tmax >= 87 or therm >= 5 else WARN if tmax >= 83 or therm > 0.5 else OK
        add("GPU", _("Temperatura y limitación térmica"), st,
            _("Máx. {tmax:.0f} °C", tmax=tmax) + (_(" · limitación térmica {therm:.1f} % del tiempo", therm=therm) if thr else ""),
            _("Si la GPU se calienta baja frecuencias y pierdes rendimiento de forma irregular."),
            [_("Revisa la curva de ventiladores y el flujo de aire de la caja."),
             _("Limpia el polvo de los disipadores."), _("Un undervolt ligero suele bajar 5-10 °C sin perder rendimiento.")] if st != OK else [])
        events += _events(valid, lambda r: (r.get("gpu_temp") or 0) >= 85 or int(r.get("gpu_throttle") or 0) & THROTTLE_THERMAL,
                          "GPU caliente/limitada", lambda ev: f"hasta {max(x.get('gpu_temp') or 0 for x in ev):.0f} °C")
    mem = a["memory"]
    vt, vu, vc = mem.get("vram_total_mb"), mem.get("vram_used_max_mb"), mem.get("vidmem_cfg_mb")
    if vt and vu:
        ratio = vu / vt
        st = BAD if ratio >= 0.97 else WARN if ratio >= 0.9 or (vc and vu > vc * 0.98) else OK
        add("GPU", _("Memoria de vídeo (VRAM)"), st,
            _("Usada máx. {p0:.1f} de {p1:.0f} GB", p0=vu / 1024, p1=vt / 1024) + (_(" · asignada a iRacing {v:.0f} GB", v=vc / 1024) if vc else ""),
            _("Si la VRAM se llena, las texturas se recargan durante la carrera y aparecen tirones y texturas borrosas."),
            [_("Sube VidMemToUseMB/VidMemMB hasta ~85-90 % de tu VRAM."),
             _("Cierra apps que usan GPU (navegador con vídeo, overlays con aceleración)."),
             _("Baja calidad de texturas o desactiva CarPaint2048x2048.")] if st != OK else [])

    # ------------------------------------------------------------------ VR
    vrd = a.get("vr")
    if vrd:
        from . import vr as _vr
        hz = vrd.get("hz") or target
        rp, at = vrd.get("reprojection_pct", 0), vrd.get("at_rate_pct", 0)
        st = BAD if rp > 25 or at < 60 else WARN if rp > 8 or at < 85 else OK
        add("VR", _("Frecuencia del visor"), st,
            _("{at:.0f} % del tiempo a {hz:.0f} Hz · {rp:.0f} % en reproyección (~{h2:.0f} fps) · {bl:.0f} % por debajo",
              at=at, hz=hz, rp=rp, h2=hz / 2, bl=vrd.get("below_pct", 0)),
            _("En VR el objetivo es mantener siempre la frecuencia del visor. Si no se llega, el runtime activa reproyección "
              "(ASW / Motion Smoothing / SSW): el juego va a la mitad y se inventan frames, con «fantasmas» en coches y bordes."),
            [_("Aplica los recortes de la pestaña Análisis (en VR suele limitar la GPU: MSAA, sombras, espejos)."),
             _("Si aún no llegas, baja la frecuencia del visor (90 → 80 o 72 Hz) o la resolución de renderizado del runtime."),
             _("Los espejos cuestan el doble en VR (se dibujan para cada ojo): usa el espejo virtual con pocos coches.")]
            if st != OK else [])
        if vrd.get("hz_detected") and vrd.get("hz_selected"):
            add("VR", _("Frecuencia real del visor"), INFO,
                _("Elegida {sel:.0f} Hz · detectada {det:.0f} Hz", sel=vrd["hz_selected"], det=vrd["hz_detected"]),
                _("Los FPS se quedan clavados en {det:.0f}: el visor estaba a {det:.0f} Hz, no a {sel:.0f}. El análisis usa {det:.0f} Hz.",
                  det=vrd["hz_detected"], sel=vrd["hz_selected"]),
                [_("Si querías {sel:.0f} Hz, cámbialo en la app del PC (Meta Horizon Link → Dispositivos → Gráficos) o en el visor, y graba otra vez.",
                   sel=vrd["hz_selected"])])
        sw = vrd.get("software") or []
        add("VR", _("Software de VR"), INFO,
            ", ".join(dict.fromkeys(x for x in ([vrd.get("runtime")] + sw) if x)) or _("no detectado"),
            _("Runtime OpenXR activo y programas de VR detectados durante la grabación."),
            _vr.runtime_tips(vrd.get("runtime"), sw))
        if a.get("presentmon"):
            add("VR", _("PresentMon en VR"), INFO, _("datos no representativos"),
                _("En VR PresentMon mide la ventana espejo del monitor, no lo que ves en el visor. Usa la frecuencia del visor de arriba."))

    # --------------------------------------------- archivo gráfico que no corresponde
    mism = a.get("renderer_mismatch")
    if mism == "vr_session_monitor_ini":
        add("VR", _("Archivo gráfico de VR"), WARN, a.get("renderer_file") or "?",
            _("La sesión es de VR, pero se leyó el archivo gráfico de monitor. En VR iRacing usa rendererDX11OpenXR.ini, así que los ajustes y perfiles mostrados no son los que estabas usando."),
            [_("Marca «Sesión en VR» antes de grabar: la app cambia sola a rendererDX11OpenXR.ini.")])
    elif mism == "monitor_session_vr_ini":
        add(_("Sistema"), _("Archivo gráfico de monitor"), WARN, a.get("renderer_file") or "?",
            _("Corriste en monitor, pero se leyó el archivo gráfico de VR, así que los ajustes y perfiles mostrados no son los que estabas usando."),
            [_("Desmarca «Sesión en VR» y elige rendererDX11Monitor.ini arriba («Archivo gráfico») antes de grabar.")])

    # ------------------------------------------------------------- frametime
    pm = a.get("presentmon") or {}
    if pm:
        spm = pm["stutters_per_min"]
        ratio = pm["low1_fps"] / pm["avg_fps"] if pm.get("avg_fps") else 1
        st = BAD if spm >= 3 or ratio < 0.5 else WARN if spm >= 1 or ratio < 0.65 else OK
        pm_causes = Counter(e["cause"] for e in pm.get("stutter_events", []))
        acts = []
        if pm_causes.get("CPU"):
            acts.append(_("Tirones de CPU: el hilo principal tardó en preparar el frame. Revisa coches dibujados, objetos de boxes y procesos en segundo plano."))
        if pm_causes.get("GPU"):
            acts.append(_("Tirones de GPU: frames puntuales muy pesados (sombras, reflejos, espejos en alto). Baja esos ajustes."))
        if pm_causes.get(_("espera/sincronización")):
            acts.append(_("Tirones sin CPU ni GPU ocupadas: carga de texturas/shaders, V-Sync o procesos que bloquean. Deja 1-2 vueltas de calentamiento, usa SSD y cierra overlays."))
        add("Frametime", _("Tirones reales (PresentMon)"), st,
            _("{p0} frames > 2× la mediana · {spm:.1f}/min · 1 % low {p2:.0f} de {p3:.0f} fps de media · peor frame {p4:.0f} ms", p0=pm['stutters_over_2x'], spm=spm, p2=pm['low1_fps'], p3=pm['avg_fps'], p4=pm['frametime_max_ms']),
            _("Medido frame a frame con PresentMon: es la medida más fiable de la suavidad. Un 1 % low muy por debajo de la media se nota como tirones aunque los FPS medios sean altos."), acts)
        for ev in pm.get("stutter_events", []):
            events.append({"kind": _("tirón ({p0})", p0=ev['cause']), "session_time": ev.get("session_time"),
                           "session_type": ev.get("session_type"), "lap_pct": ev.get("lap_pct"),
                           "duration_s": round(((ev.get("t_end") or 0) - (ev.get("t") or 0)), 2),
                           "detail": _("frame de {p0:.0f} ms (normal {p1:.1f} ms) · {p2} frames", p0=ev['max_ft'], p1=ev['base'], p2=ev['frames'])
                                     + (_(" · {n} coches cerca", n=int(ev['cars_near'])) if ev.get("cars_near") is not None else "")})
    rows_sorted = sorted(valid, key=lambda r: r["t"])
    hitches = _hitches(rows_sorted, hz)
    if not pm:
        events += hitches
    rate = len(hitches) / dur_h
    st = BAD if rate >= 20 else WARN if rate >= 6 else OK
    causes = Counter(h["cause"] for h in hitches)
    acts = []
    if causes.get(_("carga de texturas desde disco")):
        acts.append(_("Instala iRacing en un SSD NVMe y deja 1-2 vueltas de calentamiento: la primera vuelta carga texturas."))
    if causes.get(_("llegada de tráfico")):
        acts.append(_("Tirones al alcanzar coches: baja MaxCarsToDraw y coches en espejos; sube LODMinFPSTarget."))
    if causes.get(_("proceso en segundo plano")):
        acts.append(_("Un proceso en segundo plano se activó durante los tirones: revisa la lista de carga de fondo."))
    if causes.get(_("pico del hilo de física/red (BG)")) or causes.get(_("pico de red")):
        acts.append(_("Tirones de física/red: usa cable, cierra descargas y revisa la comprobación de red."))
    if causes.get(_("sin causa clara (shader/carga puntual)")):
        acts.append(_("Tirones sin causa visible: suele ser compilación de shaders o carga de un coche/objeto nuevo. Si persisten tras 2 vueltas, actualiza el driver de GPU y mide con PresentMon."))
    if causes.get("boxes"):
        acts.append(_("Tirones en boxes: baja MaxPitObjsToDraw y PitObjectDetail."))
    if not pm:
        add("Frametime", _("Tirones (caídas bruscas y breves)"), st,
            _("{n} tirones · {rate:.1f}/hora", n=len(hitches), rate=rate) + (" · " + ", ".join(f"{k}: {v}" for k, v in causes.most_common(3)) if causes else ""),
            _("Caída repentina de la media de FPS del SDK (aprox.: el SDK promedia 1 s). Para medirlos frame a frame activa PresentMon en el monitor."),
            acts if st != OK else acts[:2])

    drops = a.get("drops") or []
    if drops:
        worst = drops[0]
        st = WARN if worst["min_fps"] < target * 0.5 else OK
        add("Frametime", _("Caídas sostenidas"), st,
            _("{p0} caídas · peor {p1:.0f} fps durante {p2:.0f} s", p0=len(drops), p1=worst['min_fps'], p2=worst['duration_s']),
            _("Periodos largos por debajo del 80 % de tu mediana (salidas, pelotones, zonas pesadas del circuito). Ver «Peores caídas» y «Zonas más lentas» en el informe."),
            [_("Aplica las recomendaciones de la pestaña Análisis.")] if st != OK else [])

    # ------------------------------------------------------------------ red
    lat = [r["chan_latency"] * 1000 for r in valid if r.get("chan_latency") is not None]
    if lat:
        med_l, p95_l, max_l = median(lat), pct(lat, 95), max(lat)
        thr_l = max(med_l * 2, med_l + 60)
        spikes = _events(valid, lambda r: r.get("chan_latency") is not None and r["chan_latency"] * 1000 > thr_l,
                         _("pico de latencia"), lambda ev: f"hasta {max(x['chan_latency'] for x in ev) * 1000:.0f} ms")
        events += spikes
        srate = len(spikes) / dur_h
        ns = len(spikes)
        st = BAD if p95_l > 200 or (srate >= 20 and ns >= 10) else WARN if p95_l > 130 or (srate >= 6 and ns >= 5) else OK
        stable = (srate < 6 or ns < 5) and p95_l - med_l < 40
        if st != OK and stable:
            st = INFO  # alta pero estable: es la distancia al servidor, no tu conexión
        add(_("Red"), _("Latencia con el servidor"), st,
            _("Mediana {med_l:.0f} ms · p95 {p95_l:.0f} ms · máx. {max_l:.0f} ms · {p3} picos", med_l=med_l, p95_l=p95_l, max_l=max_l, p3=len(spikes)),
            _("Alta pero estable: es la distancia al servidor (p. ej. servidores de EE. UU. desde Europa) y no se arregla en casa. Mientras no haya picos, iRacing la compensa bien.")
            if st == INFO else
            _("La latencia depende sobre todo de la distancia al servidor; lo que importa es que sea estable. Los picos provocan saltos de coches y contactos fantasma."),
            [_("Conéctate por cable, no por Wi-Fi."), _("Pausa descargas, actualizaciones y sincronizaciones en la nube."),
             _("Si emites, limita el bitrate de subida de OBS a ~70 % de tu subida real."),
             _("Activa QoS/SQM en el router si lo soporta.")] if st in (WARN, BAD) else [])
    q = [r.get("chan_quality") for r in valid if r.get("chan_quality") is not None]
    pq = [r.get("chan_partner_quality") for r in valid if r.get("chan_partner_quality") is not None]
    if q:
        qp5, pqp5 = pct(q, 5), pct(pq, 5) if pq else None
        st = BAD if qp5 < 85 else WARN if qp5 < 95 else OK
        if pqp5 is not None and pqp5 < 95 and st == OK:
            st = INFO
        add(_("Red"), _("Calidad de conexión (pérdida de paquetes)"), st,
            f"Tuya p5 {qp5:.0f} %" + (_(" · servidor→tú p5 {pqp5:.0f} %", pqp5=pqp5) if pqp5 is not None else ""),
            _("ChanQuality baja = paquetes perdidos en tu lado; si solo baja la del servidor (partner), el problema suele estar fuera de tu casa."),
            [_("Cable de red y, si puedes, reinicia el router antes de carreras largas."),
             _("Comprueba que no haya otro equipo de la casa saturando la conexión.")] if st in (WARN, BAD) else [])
        events += _events(valid, lambda r: (r.get("chan_quality") or 100) < 90, _("pérdida de paquetes"),
                          lambda ev: _("calidad mín. {p0:.0f} %", p0=min(x['chan_quality'] for x in ev)))
    sk = [abs(r["clock_skew"]) for r in valid if r.get("clock_skew") is not None]
    if sk:
        big = 100 * sum(1 for x in sk if x > 0.1) / len(sk)
        st = WARN if big > 1 else OK
        add(_("Red"), _("Desfase de reloj con el servidor"), st, _("> 0,1 s el {big:.1f} % del tiempo · máx. {p1:.2f} s", big=big, p1=max(sk)),
            _("ChanClockSkew alto indica que tu simulación va desincronizada del servidor (CPU saturada o red inestable)."),
            [_("Revisa la carga del hilo BG y la estabilidad de la red.")] if st != OK else [])

    # ------------------------------------------------------------- memoria
    pf = [r.get("page_faults") for r in valid if r.get("page_faults") is not None]
    ram_t, ram_u = mem.get("ram_total_mb"), mem.get("ram_used_max_mb")
    if pf or ram_t:
        pf95 = pct(pf, 95) if pf else 0
        ratio = (ram_u / ram_t) if ram_t and ram_u else 0
        st = BAD if pf95 > 200 or ratio > 0.95 else WARN if pf95 > 50 or ratio > 0.88 else OK
        add(_("Memoria"), _("RAM y paginación"), st,
            _("Fallos de página p95 {pf95:.0f}/s · RAM usada máx. {p1:.0f} %", pf95=pf95, p1=ratio * 100),
            _("Si Windows tiene que paginar a disco, aparecen tirones largos y cargas lentas."),
            [_("Cierra programas que consumen mucha RAM."), _("Sube SysMemToUseMB/MaxWorkingSetMB_64Bit si iRacing se acerca al límite."),
             _("Activa el perfil XMP/EXPO de la RAM en la BIOS.")] if st != OK else [])

    # ------------------------------------------------------- configuración
    disk_log = (cfg_app.get("irsdkenabledisk") == "1") and (cfg_app.get("irsdkautologdisk") == "1")
    logging_rows = sum(1 for r in valid if r.get("disk_logging"))
    if disk_log or logging_rows:
        add(_("Sistema"), _("Telemetría grabándose en disco (.ibt)"), INFO,
            "irsdkAutoLogDisk=1" + (_(" · activo el {p0:.0f} % de la sesión", p0=100 * logging_rows / len(valid)) if logging_rows else ""),
            _("iRacing está guardando un archivo .ibt de telemetría continuamente")
            + (_(" de todos los coches (irsdkLogAllCars=1)") if cfg_app.get("irsdklogallcars") == "1" else "")
            + _(". El coste es pequeño, pero escribe en disco sin parar y llena la carpeta de telemetría."),
            [_("Si no analizas telemetría (Garage61, VRS, MoTeC…), pon irsdkAutoLogDisk=0 en app.ini; puedes activarla a mano con Alt+L."),
             _("Si la usas, pon irsdkLogAllCars=0 para grabar solo tu coche.")])

    win = hw.get("windows") or {}
    pp = (win.get("power_plan") or {}).get("name")
    if pp:
        st = WARN if pp == "Economizador" else OK
        add(_("Sistema"), _("Plan de energía"), st, _(pp),
            _("Con 'Economizador' la CPU no sube frecuencias."), [_("Usa 'Equilibrado' (recomendado por AMD para Ryzen) o 'Alto rendimiento'.")] if st != OK else [])
    if win.get("hags") is False:
        add(_("Sistema"), _("Programación de GPU acelerada por hardware"), INFO, _("Desactivada"),
            _("En algunos equipos mejora la latencia y la estabilidad del frametime."),
            [_("Pruébala activada (Configuración → Pantalla → Gráficos) y compara dos sesiones con «Comparar».")])
    vs = cfg_r.get("verticalsync")
    lim = cfg_r.get("limitframerate")
    if vs == "1":
        add(_("Sistema"), _("V-Sync de iRacing activado"), WARN, "VerticalSync=1",
            _("Con G-Sync/FreeSync conviene V-Sync del juego desactivado y tope de FPS unos fps por debajo de la frecuencia."),
            [_("Pon VerticalSync=0, activa V-Sync en el panel del driver y usa LimitFrameRate=1 con un tope por debajo de los Hz.")])
    elif lim == "0":
        add(_("Sistema"), _("Sin tope de FPS"), WARN, "LimitFrameRate=0",
            _("Sin tope los FPS oscilan mucho y el input lag varía."), [_("Activa el tope (ver recomendaciones de Análisis).")])

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
                    self._emit("hitch", t, _("Tirón: {base:.0f} → {fps:.0f} fps (coches cerca: {p2})", base=base, fps=fps, p2=r.get('cars_near', '?')), out)
            self.fps_hist.append(fps)
            self.fps_hist = self.fps_hist[-40:]
        lat = _f(r.get("chan_latency"))
        if lat and lat > 0.15:
            self._emit("lat", t, _("Pico de latencia: {p0:.0f} ms", p0=lat * 1000), out)
        q = _f(r.get("chan_quality"))
        if q is not None:
            q = q * 100 if q <= 1.5 else q
            if q < 90:
                self._emit("q", t, _("Pérdida de paquetes: calidad {q:.0f} %", q=q), out)
        bg = _f(r.get("cpu_bg"))
        if bg is not None:
            bg = bg * 100 if bg <= 1.5 else bg
            if bg >= 85:
                self._emit("bg", t, _("Hilo de física/red (BG) al {bg:.0f} %", bg=bg), out)
        gt = _f(r.get("gpu_temp"))
        if gt and gt >= 85:
            self._emit("temp", t, f"GPU a {gt:.0f} °C", out)
        thr = r.get("gpu_throttle")
        if thr is not None and int(thr) & THROTTLE_THERMAL:
            self._emit("thr", t, _("GPU limitando frecuencia por temperatura"), out)
        pf = _f(r.get("page_faults"))
        if pf and pf > 200:
            self._emit("pf", t, _("Paginación a disco: {pf:.0f} fallos/s", pf=pf), out)
        return out
