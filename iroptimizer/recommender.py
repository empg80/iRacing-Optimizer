"""Motor de recomendaciones: convierte el análisis en cambios concretos de los .ini.

Filosofía:
  1. Primero estabilidad (frametime constante) y luego media de FPS.
  2. Atacar el recurso que limita: si el hilo principal (CpuUsageFG) está saturado,
     bajar gráficos de GPU no sirve de casi nada, y al revés.
  3. Cambios pequeños y priorizados: se propone el mínimo que debería cerrar el déficit.
Prioridad: 1 = aplicar ya, 2 = recomendable, 3 = opcional / informativo.
"""
from __future__ import annotations

from .i18n import _

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

    vrd = a.get("vr")
    if vrd:
        hz = vrd.get("hz") or target
        if vrd.get("reprojection_pct", 0) > 10:
            b.info(_("Reproyección activa en VR"),
                   _("El {p:.0f} % del tiempo estás a ~{h:.0f} fps (la mitad de {hz:.0f} Hz): el runtime inventa uno de cada dos "
                     "frames y aparecen «fantasmas» en coches y bordes. Aplica los recortes de abajo o baja la frecuencia del "
                     "visor: mejor {lower} Hz estables que {hz:.0f} Hz con reproyección.",
                     p=vrd["reprojection_pct"], h=hz / 2, hz=hz, lower=80 if hz > 80 else 72), 1, "frametime")
        elif vrd.get("at_rate_pct", 100) < 85:
            b.info(_("No llegas a la frecuencia del visor"),
                   _("Solo el {p:.0f} % del tiempo a {hz:.0f} Hz. En VR cada caída se nota mucho más que en monitor.",
                     p=vrd["at_rate_pct"], hz=hz), 2, "frametime")

    # ------------------------------------------------------------------ FPS CAP
    lim = b.cur("LimitFrameRate") if not vrd else None
    desired = b.cur("DesiredFPSLimit")
    vrr_cap = (refresh - 3) if refresh else None
    holdable = _round_down(fps["p10"])  # lo que sostienes el 90 % del tiempo
    if lim == 0:
        cap = min(vrr_cap or holdable, max(60, holdable))
        b.add("LimitFrameRate", 1, 1, "frametime",
              _("Sin límite de FPS tus FPS oscilan entre {p0:.0f} (p5) y {p1:.0f} (p95) en {lbl}. Un tope que puedas sostener da frametimes constantes y menos picos de input lag.", p0=fps['p5'], p1=fps['p95'], lbl=lbl))
        b.add("DesiredFPSLimit", cap, 1, "frametime",
              _("Tope propuesto: {cap} fps ≈ lo que mantienes el 90 % del tiempo", cap=cap)
              + (_(", y por debajo de {refresh:.0f} Hz para quedarte dentro de G-Sync/VRR.", refresh=refresh) if vrr_cap else "."))
    elif lim == 1 and desired and not vrd:
        if refresh and desired > refresh - 2:
            b.add("DesiredFPSLimit", vrr_cap, 2, "frametime",
                  _("Con VRR conviene limitar unos fps por debajo de la frecuencia ({refresh:.0f} Hz) para no salir del rango G-Sync y caer en V-Sync.", refresh=refresh))
        elif fps["p10"] < desired * 0.9 and holdable >= 60:
            if bn["cap"] >= 20:
                # Pasas bastante tiempo en el tope: bajarlo hasta p10 tiraría FPS útiles en las zonas limpias.
                mid = _round_down(fps.get("p25") or fps["p10"])
                if desired > mid * 1.15:
                    b.add("DesiredFPSLimit", mid, 3, "frametime",
                          _("Estás en el tope el {p0:.0f} % del tiempo y caes a {p1:.0f} en tráfico. Un tope de {mid} (lo que mantienes el 75 % del tiempo) reduce la diferencia entre zonas limpias y pelotones sin renunciar a tanto. Con G-Sync es opcional.", p0=bn['cap'], p1=fps['p10'], mid=mid))
            else:
                b.add("DesiredFPSLimit", holdable, 2, "frametime",
                      _("Tu tope ({desired:.0f}) está muy por encima de lo que sostienes en {lbl} (p10 {p2:.0f}). Bajarlo a {holdable} alisa el frametime. Mejor orden: aplica antes los recortes de CPU, vuelve a grabar y fija el tope con el nuevo p10.", desired=desired, lbl=lbl, p2=fps['p10'], holdable=holdable))

    # ------------------------------------------------------------- CPU (hilo FG)
    # Se activa si la CPU domina en global o si provoca la mayoría de las peores caídas (salidas, pelotones)
    if deficit and ((cpu_share >= 20 and cpu_share >= gpu_share * 0.8) or (drop_cpu >= 0.5 and len(drops) >= 3)):
        heavy_traffic = (a.get("traffic_drop_pct") or 0) > 12
        n = b.cur("MaxCarsToDrawInMirrors")
        if n is not None and n > 8:
            b.add("MaxCarsToDrawInMirrors", 8 if severity < 0.2 else 6, 1, "cpu",
                  _("Cada espejo es otra cámara que renderiza coches: es el recorte con mejor relación coste/beneficio para el hilo principal en salidas y pelotones."))
        n = b.cur("MaxCarsToDraw")
        if n is not None:
            goal = 20 if heavy_traffic and severity > 0.2 else 24 if heavy_traffic else 28
            if n > goal:
                drop = a.get("traffic_drop_pct")
                b.add("MaxCarsToDraw", goal, 1, "cpu",
                      _("Limitado por CPU/motor (GPU esperando) el {cpu_share:.0f} % del tiempo y en el {p1:.0f} % de las peores caídas", cpu_share=cpu_share, p1=100*drop_cpu)
                      + (_(" y pierdes un {drop:.0f} % de FPS con tráfico cercano.", drop=drop) if drop else ".")
                      + _(" Es solo visual: no cambia los coches que recibes por red ni la telemetría."))
        for key, val, pr, why in (
            ("MaxPitObjsToDrawInMirrors", 0, 1, _("Objetos de boxes en espejos: coste de CPU sin valor en carrera.")),
            ("MaxPitObjsToDraw", 10, 2, _("Menos objetos de boxes dibujados, alivia la CPU sobre todo en la salida y al entrar a boxes.")),
        ):
            c = b.cur(key)
            if c is not None and c > val:
                b.add(key, val, pr, "cpu", why)
        if b.cur("ParallelSorting") == 0:
            b.add("ParallelSorting", 1, 1, "cpu", _("Ordena la escena en varios hilos: descarga el hilo principal."))
        if b.cur("OcclusionCull") == 0:
            b.add("OcclusionCull", 1, 1, "cpu", _("El occlusion culling evita dibujar lo que no se ve; casi siempre compensa."))
        c = b.cur("DynamicLODSpacing")
        if c is not None and c < 5:
            b.add("DynamicLODSpacing", 5, 2, "cpu",
                  _("Reduce el LOD de coches/crew cuando hay muchos en alta calidad (salidas, pelotones)."))
        c = b.cur("LODMinFPSTarget")
        lod_goal = _round_down(min(reach, fps["median"]), 10)
        if c is not None and c < lod_goal * 0.85:
            b.add("LODMinFPSTarget", lod_goal, 1, "cpu",
                  _("Está en {c:.0f}: el LOD dinámico nunca actúa porque nunca bajas de ahí. Con {lod_goal} iRacing reduce el detalle de coches y objetos justo en los momentos de carga (tráfico, zonas pesadas) y lo recupera después.", c=c, lod_goal=lod_goal))
        for key, why in (("CrowdDetail", _("Público: muchas draw calls en Le Mans, Spa, Nordschleife.")),
                         ("GrandstandDetail", _("Gradas: mismo motivo, coste de CPU en rectas principales.")),
                         ("PitObjectDetail", _("Detalle de boxes: las caídas en pit lane vienen de aquí y de MaxPitObjsToDraw."))):
            c = b.cur(key)
            if c is not None and c >= 1 and severity > 0.15:
                b.add(key, c - 1, 2, "cpu", why)
        c = b.cur("MaxCockpitMirrors")
        if severity > 0.2 and c is not None and c > 2:
            b.add("MaxCockpitMirrors", 2, 3, "cpu", _("Cada espejo extra es un render adicional. Úsalo si dependes del espejo virtual o del spotter."))

    # -------------------------------------------------------------------- GPU
    if deficit and gpu_share >= 20 and gpu_share > cpu_share * 0.8:
        steps = [
            ("NumDynamicCubemaps", lambda v: 0 if v > 0 else None, 1, _("Reflejos dinámicos: caros en GPU y poco visibles conduciendo.")),
            ("MirrorDetail", lambda v: 0 if v >= 1 else None, 1, _("Espejos en detalle bajo: poca diferencia visible, ahorro claro.")),
            ("ShadowMapType", lambda v: v - 1 if v >= 3 else None, 1, _("Quitar sombras de objetos de pista (tso) y dejar pista+coches.")),
            ("ShaderQuality", lambda v: v - 1 if v >= 3 else None, 2, _("Shader 'max' → 'high': ahorro de GPU con impacto visual pequeño.")),
            ("ParticleDetail", lambda v: v - 1 if v >= 2 else None, 2, _("Partículas (humo, polvo, lluvia) a medio.")),
            ("TwoPassTrees", lambda v: 0 if v >= 1 else None, 2, _("Árboles en una pasada.")),
            ("DynamicTrackTextureUpdateRate", lambda v: v - 1 if v >= 3 else None, 2, _("Menos actualizaciones de textura dinámica de pista.")),
            ("HeadlightLevel", lambda v: v - 1 if v >= 2 else None, 3, _("Faros a calidad media (relevante de noche).")),
            ("NumFixedCubemaps", lambda v: 0 if v > 0 else None, 3, "Reflejos fijos fuera."),
            ("MaxAniso", lambda v: 8 if v > 8 else None, 3, _("Anisotrópico x8: ganancia mínima, solo si vas muy justo.")),
        ]
        budget = 2 if severity < 0.1 else 4 if severity < 0.25 else len(steps)
        for key, fn, pr, why in steps:
            if budget <= 0:
                break
            c = b.cur(key)
            new = fn(c) if c is not None else None
            if new is not None:
                b.add(key, new, pr, "gpu", _("GPU al límite el {gpu_share:.0f} % del tiempo en {lbl}. {why}", gpu_share=gpu_share, lbl=lbl, why=why))
                budget -= 1

    # ----------------------------------------------------------- Margen para subir calidad
    if not deficit and fps["p5"] >= target * 0.98:
        gp95 = (g.get("gpu") or {}).get("p95") or 100
        cp95 = (g.get("cpu_fg") or {}).get("p95") or 100
        if gp95 < 75 and cp95 < 70:
            n = b.cur("MaxCarsToDraw")
            if n is not None and n < 40:
                b.add("MaxCarsToDraw", min(40, n + 10), 3, "calidad", _("Tienes margen de CPU: puedes ver más coches sin bajar del objetivo."))
            if b.cur("MirrorDetail") == 0:
                b.add("MirrorDetail", 1, 3, "calidad", _("Tienes margen de GPU: espejos en alto."))
            if (b.cur("ShadowMapType") or 3) < 3:
                b.add("ShadowMapType", 3, 3, "calidad", _("Tienes margen de GPU: sombras completas."))

    # -------------------------------------------------------------- Memoria
    m = a["memory"]
    vt, vcfg = m.get("vram_total_mb"), m.get("vidmem_cfg_mb")
    if vt and vcfg is not None and (vcfg < vt * 0.6 or vcfg > vt):
        b.add(m.get("vidmem_key", "VidMemMB"), int(vt * 0.85), 3, "memoria",
              _("Tienes {p0:.0f} GB de VRAM y iRacing está configurado para usar {vcfg:.0f} MB. iRacing recomienda ajustarlo a tu hardware; ~90 % deja margen al sistema y a overlays.", p0=vt/1024, vcfg=vcfg))
    rt, wcfg, rss = m.get("ram_total_mb"), m.get("maxworkingset_cfg_mb"), m.get("ir_rss_max_mb")
    if rt and wcfg is not None:
        if (rss and rss > wcfg * 0.9) or wcfg < min(rt * 0.35, 16384):
            b.add(m.get("sysmem_key", "MaxWorkingSetMB_64Bit"), int(min(rt * 0.5, 32768)), 2, "memoria",
                  _("Límite de memoria del sim ({wcfg:.0f} MB) bajo para tus {p1:.0f} GB de RAM", wcfg=wcfg, p1=rt/1024)
                  + (_("; el proceso llegó a {rss:.0f} MB.", rss=rss) if rss else "."))

    # -------------------------------------------------------------- Red
    mc = _num(b.app.get("maxcars") or b.app.get("servertransmitmaxcars"))
    if mc is not None and mc < 63:
        b.add("serverTransmitMaxCars" if b.app.get("servertransmitmaxcars") is not None else "maxCars", 63, 2, "red",
              _("Max Cars es un ajuste de red: limita los coches que te envía el servidor. Bajarlo deja sin datos a overlays y telemetría (relatives, estrategia de combustible). Para rendimiento usa MaxCarsToDraw."), file="app")

    # -------------------------------------------------------------- Latencia y boxes
    if b.cur("NvReflexMode") == 0:
        b.add("NvReflexMode", 1, 2, "frametime",
              _("NVIDIA Reflex desactivado. Al ir limitado por CPU, Reflex vacía la cola de render y reduce el input lag sin coste de FPS. Compatible con G-Sync y con el tope de iRacing."))
    fp, ft = (g.get("fps_pit") or {}), (g.get("fps_track") or {})
    if fp.get("n", 0) > 40 and ft.get("mean") and fp["mean"] < ft["mean"] * 0.75:
        b.info(_("Caída en pit lane"), _("{p0:.0f} fps en boxes frente a {p1:.0f} en pista. Lo atacan MaxPitObjsToDraw y PitObjectDetail.", p0=fp['mean'], p1=ft['mean']), 3, "cpu")

    b.recs.sort(key=lambda r: (r["priority"], r.get("category") or ""))
    return b.recs
