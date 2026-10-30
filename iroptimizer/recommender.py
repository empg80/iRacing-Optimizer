"""Motor de recomendaciones: convierte el análisis en cambios concretos de los .ini.

Filosofía:
  1. Primero estabilidad (frametime constante) y luego media de FPS.
  2. Atacar el recurso que limita: si el hilo principal (CpuUsageFG) está saturado,
     bajar gráficos de GPU no sirve de casi nada, y al revés.
  3. Cambios pequeños y priorizados: se propone el mínimo que debería cerrar el déficit.
Prioridad: 1 = aplicar ya, 2 = recomendable, 3 = opcional / informativo.
"""
from __future__ import annotations

import math

from .config_reader import flat_from_snapshot


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _section(snapshot_part: dict, key: str) -> str | None:
    for sec, kv in snapshot_part.items():
        for k in kv:
            if k.lower() == key.lower():
                return sec
    return None


class Builder:
    def __init__(self, analysis: dict, config_snapshot: dict):
        self.a = analysis
        self.snap = config_snapshot or {}
        self.r = flat_from_snapshot(self.snap.get("renderer", {}))
        self.app = flat_from_snapshot(self.snap.get("app", {}))
        self.recs: list[dict] = []
        self._keys: set[str] = set()

    def cur(self, key):
        return _num(self.r.get(key.lower()))

    def add(self, key, proposed, priority, category, reason, file="renderer"):
        if key.lower() in self._keys:
            return
        src = self.r if file == "renderer" else self.app
        current = src.get(key.lower())
        if current is None:
            return  # la clave no existe en tu archivo: no inventamos nada
        if _num(current) is not None and _num(current) == _num(proposed):
            return
        self._keys.add(key.lower())
        self.recs.append({
            "file": file, "file_name": self.snap.get("renderer_file") if file == "renderer" else "app.ini",
            "section": _section(self.snap.get(file, {}), key), "key": key,
            "current": current, "proposed": str(int(proposed)) if isinstance(proposed, (int, float)) and float(proposed).is_integer() else str(proposed),
            "priority": priority, "category": category, "reason": reason,
        })

    def info(self, title, text, priority=3, category="sistema"):
        self.recs.append({"file": None, "key": None, "title": title, "reason": text,
                          "priority": priority, "category": category})


def _round_down(x, step=5):
    return int(math.floor(x / step) * step)


def build(analysis: dict, config_snapshot: dict) -> list[dict]:
    b = Builder(analysis, config_snapshot)
    a = analysis
    g = a["groups"][a["primary"]]
    fps = g["fps"]
    target = a["target_fps"]
    refresh = _num((a["hardware"].get("display") or {}).get("refresh"))
    bn = g["bottleneck"]
    cpu_share = bn["cpu"] + bn["mixed"] / 2
    gpu_share = bn["gpu"] + bn["mixed"] / 2
    reach = min(target, fps["p95"])  # lo que alcanzas en condiciones buenas (evita exagerar con topes inalcanzables)
    severity = max(0.0, (reach - fps["p5"]) / reach)
    deficit = g["below_target_pct"] > 10 or severity > 0.05
    lbl = a["primary"]
    drops = a.get("drops") or []
    drop_cpu = sum(1 for d in drops if d["bottleneck"] in ("cpu", "mixed")) / len(drops) if drops else 0

    # ------------------------------------------------------------------ FPS CAP
    lim = b.cur("LimitFrameRate")
    desired = b.cur("DesiredFPSLimit")
    vrr_cap = (refresh - 3) if refresh else None
    holdable = _round_down(fps["p10"])  # lo que sostienes el 90 % del tiempo
    if lim == 0:
        cap = min(vrr_cap or holdable, max(60, holdable))
        b.add("LimitFrameRate", 1, 1, "frametime",
              f"Sin límite de FPS tus FPS oscilan entre {fps['p5']:.0f} (p5) y {fps['p95']:.0f} (p95) en {lbl}. "
              "Un tope que puedas sostener da frametimes constantes y menos picos de input lag.")
        b.add("DesiredFPSLimit", cap, 1, "frametime",
              f"Tope propuesto: {cap} fps ≈ lo que mantienes el 90 % del tiempo"
              + (f", y por debajo de {refresh:.0f} Hz para quedarte dentro de G-Sync/VRR." if vrr_cap else "."))
    elif lim == 1 and desired:
        if refresh and desired > refresh - 2:
            b.add("DesiredFPSLimit", vrr_cap, 2, "frametime",
                  f"Con VRR conviene limitar unos fps por debajo de la frecuencia ({refresh:.0f} Hz) "
                  "para no salir del rango G-Sync y caer en V-Sync.")
        elif fps["p10"] < desired * 0.9 and holdable >= 60:
            b.add("DesiredFPSLimit", holdable, 2, "frametime",
                  f"Tu tope ({desired:.0f}) está muy por encima de lo que sostienes en {lbl} (p10 {fps['p10']:.0f}). "
                  f"Bajarlo a {holdable} alisa el frametime. Mejor orden: aplica antes los recortes de CPU, vuelve a grabar "
                  "y fija el tope con el nuevo p10.")

    # ------------------------------------------------------------- CPU (hilo FG)
    # Se activa si la CPU domina en global o si provoca la mayoría de las peores caídas (salidas, pelotones)
    if deficit and ((cpu_share >= 20 and cpu_share >= gpu_share * 0.8) or (drop_cpu >= 0.5 and len(drops) >= 3)):
        heavy_traffic = (a.get("traffic_drop_pct") or 0) > 12
        n = b.cur("MaxCarsToDrawInMirrors")
        if n is not None and n > 8:
            b.add("MaxCarsToDrawInMirrors", 8 if severity < 0.2 else 6, 1, "cpu",
                  "Cada espejo es otra cámara que renderiza coches: es el recorte con mejor relación coste/beneficio "
                  "para el hilo principal en salidas y pelotones.")
        n = b.cur("MaxCarsToDraw")
        if n is not None:
            goal = 20 if heavy_traffic and severity > 0.2 else 24 if heavy_traffic else 28
            if n > goal:
                drop = a.get("traffic_drop_pct")
                b.add("MaxCarsToDraw", goal, 1, "cpu",
                      f"Limitado por CPU/motor (GPU esperando) el {cpu_share:.0f} % del tiempo y en el {100*drop_cpu:.0f} % de las peores caídas"
                      + (f" y pierdes un {drop:.0f} % de FPS con tráfico cercano." if drop else ".")
                      + " Es solo visual: no cambia los coches que recibes por red ni la telemetría.")
        for key, val, pr, why in (
            ("MaxPitObjsToDrawInMirrors", 0, 1, "Objetos de boxes en espejos: coste de CPU sin valor en carrera."),
            ("MaxPitObjsToDraw", 10, 2, "Menos objetos de boxes dibujados, alivia la CPU sobre todo en la salida y al entrar a boxes."),
        ):
            c = b.cur(key)
            if c is not None and c > val:
                b.add(key, val, pr, "cpu", why)
        if b.cur("ParallelSorting") == 0:
            b.add("ParallelSorting", 1, 1, "cpu", "Ordena la escena en varios hilos: descarga el hilo principal.")
        if b.cur("OcclusionCull") == 0:
            b.add("OcclusionCull", 1, 1, "cpu", "El occlusion culling evita dibujar lo que no se ve; casi siempre compensa.")
        c = b.cur("DynamicLODSpacing")
        if c is not None and c < 5:
            b.add("DynamicLODSpacing", 5, 2, "cpu",
                  "Reduce el LOD de coches/crew cuando hay muchos en alta calidad (salidas, pelotones).")
        c = b.cur("LODMinFPSTarget")
        lod_goal = _round_down(min(reach, fps["median"]), 10)
        if c is not None and c < lod_goal * 0.85:
            b.add("LODMinFPSTarget", lod_goal, 1, "cpu",
                  f"Está en {c:.0f}: el LOD dinámico nunca actúa porque nunca bajas de ahí. Con {lod_goal} iRacing reduce el detalle "
                  "de coches y objetos justo en los momentos de carga (tráfico, zonas pesadas) y lo recupera después.")
        for key, why in (("CrowdDetail", "Público: muchas draw calls en Le Mans, Spa, Nordschleife."),
                         ("GrandstandDetail", "Gradas: mismo motivo, coste de CPU en rectas principales."),
                         ("PitObjectDetail", "Detalle de boxes: las caídas en pit lane vienen de aquí y de MaxPitObjsToDraw.")):
            c = b.cur(key)
            if c is not None and c >= 1 and severity > 0.15:
                b.add(key, c - 1, 2, "cpu", why)
        c = b.cur("MaxCockpitMirrors")
        if severity > 0.2 and c is not None and c > 2:
            b.add("MaxCockpitMirrors", 2, 3, "cpu", "Cada espejo extra es un render adicional. Úsalo si dependes del espejo virtual o del spotter.")

    # -------------------------------------------------------------------- GPU
    if deficit and gpu_share >= 20 and gpu_share > cpu_share * 0.8:
        steps = [
            ("NumDynamicCubemaps", lambda v: 0 if v > 0 else None, 1, "Reflejos dinámicos: caros en GPU y poco visibles conduciendo."),
            ("MirrorDetail", lambda v: 0 if v >= 1 else None, 1, "Espejos en detalle bajo: poca diferencia visible, ahorro claro."),
            ("ShadowMapType", lambda v: v - 1 if v >= 3 else None, 1, "Quitar sombras de objetos de pista (tso) y dejar pista+coches."),
            ("ShaderQuality", lambda v: v - 1 if v >= 3 else None, 2, "Shader 'max' → 'high': ahorro de GPU con impacto visual pequeño."),
            ("ParticleDetail", lambda v: v - 1 if v >= 2 else None, 2, "Partículas (humo, polvo, lluvia) a medio."),
            ("TwoPassTrees", lambda v: 0 if v >= 1 else None, 2, "Árboles en una pasada."),
            ("DynamicTrackTextureUpdateRate", lambda v: v - 1 if v >= 3 else None, 2, "Menos actualizaciones de textura dinámica de pista."),
            ("HeadlightLevel", lambda v: v - 1 if v >= 2 else None, 3, "Faros a calidad media (relevante de noche)."),
            ("NumFixedCubemaps", lambda v: 0 if v > 0 else None, 3, "Reflejos fijos fuera."),
            ("MaxAniso", lambda v: 8 if v > 8 else None, 3, "Anisotrópico x8: ganancia mínima, solo si vas muy justo."),
        ]
        budget = 2 if severity < 0.1 else 4 if severity < 0.25 else len(steps)
        for key, fn, pr, why in steps:
            if budget <= 0:
                break
            c = b.cur(key)
            new = fn(c) if c is not None else None
            if new is not None:
                b.add(key, new, pr, "gpu", f"GPU al límite el {gpu_share:.0f} % del tiempo en {lbl}. {why}")
                budget -= 1

    # ----------------------------------------------------------- "other": ni CPU ni GPU al límite
    if deficit and bn["other"] > 40:
        bg = (g.get("cpu_bg") or {}).get("p95")
        b.info("Caídas sin CPU ni GPU saturadas",
               f"El {bn['other']:.0f} % del tiempo estás por debajo del objetivo sin que el hilo principal ni la GPU estén al límite"
               + (f" (CpuUsageBG p95 {bg:.0f} %)" if bg else "")
               + ". Suele ser sincronización (V-Sync/tope del driver), overlays (SimHub, OBS, Discord) o procesos en segundo plano. "
               "Prueba a cerrar overlays uno a uno y revisa que no haya un límite de FPS en el panel de NVIDIA.", 2)

    # ----------------------------------------------------------- Margen para subir calidad
    if not deficit and fps["p5"] >= target * 0.98:
        gp95 = (g.get("gpu") or {}).get("p95") or 100
        cp95 = (g.get("cpu_fg") or {}).get("p95") or 100
        if gp95 < 75 and cp95 < 70:
            n = b.cur("MaxCarsToDraw")
            if n is not None and n < 40:
                b.add("MaxCarsToDraw", min(40, n + 10), 3, "calidad", "Tienes margen de CPU: puedes ver más coches sin bajar del objetivo.")
            if b.cur("MirrorDetail") == 0:
                b.add("MirrorDetail", 1, 3, "calidad", "Tienes margen de GPU: espejos en alto.")
            if (b.cur("ShadowMapType") or 3) < 3:
                b.add("ShadowMapType", 3, 3, "calidad", "Tienes margen de GPU: sombras completas.")

    # -------------------------------------------------------------- Memoria
    m = a["memory"]
    vt, vcfg = m.get("vram_total_mb"), m.get("vidmem_cfg_mb")
    if vt and vcfg is not None and (vcfg < vt * 0.6 or vcfg > vt):
        b.add(m.get("vidmem_key", "VidMemMB"), int(vt * 0.85), 3, "memoria",
              f"Tienes {vt/1024:.0f} GB de VRAM y iRacing está configurado para usar {vcfg:.0f} MB. "
              "iRacing recomienda ajustarlo a tu hardware; ~90 % deja margen al sistema y a overlays.")
    rt, wcfg, rss = m.get("ram_total_mb"), m.get("maxworkingset_cfg_mb"), m.get("ir_rss_max_mb")
    if rt and wcfg is not None:
        if (rss and rss > wcfg * 0.9) or wcfg < min(rt * 0.35, 16384):
            b.add(m.get("sysmem_key", "MaxWorkingSetMB_64Bit"), int(min(rt * 0.5, 32768)), 2, "memoria",
                  f"Límite de memoria del sim ({wcfg:.0f} MB) bajo para tus {rt/1024:.0f} GB de RAM"
                  + (f"; el proceso llegó a {rss:.0f} MB." if rss else "."))
    pf = m.get("page_faults_p95")
    if pf and pf > 200:
        b.info("Fallos de página altos", f"MemPageFaultSec p95 = {pf:.0f}/s. Indica paginación a disco: "
               "revisa RAM libre, cierra programas pesados y sube MaxWorkingSetMB_64Bit.", 2, "memoria")

    # -------------------------------------------------------------- Red
    mc = _num(b.app.get("maxcars") or b.app.get("servertransmitmaxcars"))
    if mc is not None and mc < 63:
        b.add("serverTransmitMaxCars" if b.app.get("servertransmitmaxcars") is not None else "maxCars", 63, 2, "red",
              "Max Cars es un ajuste de red: limita los coches que te envía el servidor. Bajarlo deja sin datos a overlays "
              "y telemetría (relatives, estrategia de combustible). Para rendimiento usa MaxCarsToDraw.", file="app")
    n = a["network"]
    if (n.get("quality_p5") is not None and n["quality_p5"] < 90) or (n.get("latency_p95_ms") or 0) > 150:
        b.info("Calidad de conexión", f"ChanQuality p5 {n.get('quality_p5') or 0:.0f} %, latencia p95 {n.get('latency_p95_ms') or 0:.0f} ms. "
               "Usa cable, pausa descargas y comprueba el bitrate de OBS si emites.", 2, "red")

    # -------------------------------------------------------------- Latencia / GPU de sobra / fondo
    if b.cur("NvReflexMode") == 0:
        b.add("NvReflexMode", 1, 2, "frametime",
              "NVIDIA Reflex desactivado. Al ir limitado por CPU, Reflex vacía la cola de render y reduce el input lag "
              "sin coste de FPS. Compatible con G-Sync y con el tope de iRacing.")
    gp95 = (g.get("gpu") or {}).get("p95")
    if cpu_share > 60 and gp95 is not None and gp95 < 75:
        pw = (g.get("gpu_power") or {}).get("median")
        b.info("GPU infrautilizada",
               f"La GPU no pasa del {gp95:.0f} % (p95)" + (f" y consume ~{pw:.0f} W" if pw else "")
               + ". No bajes ajustes de GPU (MSAA, sombras, shaders): no ganarías FPS. Al revés, tienes margen para subirlos "
               "casi gratis una vez estabilices la CPU.", 2, "gpu")
    sca = (g.get("sys_cpu_avg") or {}).get("median")
    cores = ((a["hardware"].get("cpu") or {}).get("cores_logical")) or 0
    if sca and sca > 35:
        b.info("Carga de CPU de fondo alta",
               f"Uso medio de todos los hilos: {sca:.0f} %" + (f" (≈ {sca/100*cores:.0f} de {cores} hilos ocupados)" if cores else "")
               + ". iRacing usa pocos hilos; algo más está consumiendo CPU (OBS, SimHub, navegador, antivirus, RGB…). "
               "Mira el Administrador de tareas con iRacing en pista y cierra lo que no uses: compite por la caché del X3D.", 1, "sistema")
    fp, ft = (g.get("fps_pit") or {}), (g.get("fps_track") or {})
    if fp.get("n", 0) > 40 and ft.get("mean") and fp["mean"] < ft["mean"] * 0.75:
        b.info("Caída en pit lane", f"{fp['mean']:.0f} fps en boxes frente a {ft['mean']:.0f} en pista. Lo atacan MaxPitObjsToDraw y PitObjectDetail.", 3, "cpu")

    # -------------------------------------------------------------- Sistema
    win = a["hardware"].get("windows") or {}
    pp = win.get("power_plan") or {}
    if pp and pp.get("name") == "Economizador":
        b.info("Plan de energía", "Estás en 'Economizador'. Cambia a 'Equilibrado' (Ryzen) o 'Alto rendimiento'.", 1)
    if win.get("hags") is False:
        b.info("Programación de GPU acelerada por hardware", "Está desactivada. Pruébala activada (requiere reinicio) y compara con otra sesión.", 3)
    t = a["thermal"].get("gpu_temp_max")
    if t and t >= 83:
        b.info("Temperatura de GPU", f"Máxima {t:.0f} °C: posible bajada de frecuencias por temperatura. Revisa curva de ventiladores/flujo de aire.", 2, "sistema")
    pm = a.get("presentmon")
    if pm and pm.get("low1_fps") and pm["low1_fps"] < target * 0.75:
        b.info("Stutter (PresentMon)", f"1 % low {pm['low1_fps']:.0f} fps frente a media {pm['avg_fps']:.0f}. "
               "Los picos aislados suelen venir de carga de texturas o shaders: deja 1-2 vueltas de calentamiento y mira si persisten.", 2, "frametime")

    b.recs.sort(key=lambda r: (r["priority"], r.get("category") or ""))
    return b.recs
