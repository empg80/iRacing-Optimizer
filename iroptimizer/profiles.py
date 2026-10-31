"""Tres perfiles genéricos: Rendimiento, Equilibrado y Calidad.

Cada perfil es una tabla de valores para claves de rendererDX11*.ini. Solo se aplican las claves que
existen en el archivo del usuario. El tope de FPS y el LOD dinámico se ajustan con el análisis
(si existe) y con los Hz de la pantalla. `None` = no tocar.

Notas de diseño:
- Nunca se quitan espejos (MaxCockpitMirrors/VirtualMirrors): es seguridad en pista.
- Las claves de valor dudoso (rangos no documentados) no se incluyen.
"""
from __future__ import annotations

from .i18n import _

import math

PROFILES = {
    "rendimiento": {
        "title": _("Máximo rendimiento"),
        "desc": _("Prioriza FPS estables en salidas y pelotones. Menos coches y objetos dibujados, sombras y efectos al mínimo."),
        "values": {
            "MaxCarsToDraw": 16, "MaxCarsToDrawInMirrors": 4,
            "MaxPitObjsToDraw": 0, "MaxPitObjsToDrawInMirrors": 0,
            "CrowdDetail": 0, "GrandstandDetail": 0, "PitObjectDetail": 0, "ObjectDetail": 0, "FoliageDetail": 0,
            "CarDetail": 1, "EnableSwayTrees": 0,
            "ShadowMapType": 1, "ShadowDetail": 0, "DynamicShadowMaps": 0,
            "MirrorDetail": 0, "ParticleDetail": 1, "ShaderQuality": 1, "TwoPassTrees": 0,
            "NumDynamicCubemaps": 0, "NumFixedCubemaps": 0, "SSAO": 0, "SSRLevel": 0,
            "HeatHaze": 0, "DepthOfField": 0, "HeadlightLevel": 0, "SkyRefreshRate": 0,
            "MSAASamples": 2, "MaxAniso": 8, "DynamicTrackTextureUpdateRate": 1,
            "ParallelSorting": 1, "OcclusionCull": 1, "NvReflexMode": 1,
        },
        "lod_factor": 0.95, "cap_factor": 1.15,
    },
    "equilibrado": {
        "title": _("Equilibrado"),
        "desc": _("Buena imagen sin castigar la CPU: coches suficientes, sombras de pista y coches, efectos moderados."),
        "values": {
            "MaxCarsToDraw": 24, "MaxCarsToDrawInMirrors": 6,
            "MaxPitObjsToDraw": 10, "MaxPitObjsToDrawInMirrors": 0,
            "CrowdDetail": 1, "GrandstandDetail": 1, "PitObjectDetail": 1, "ObjectDetail": 1, "FoliageDetail": 1,
            "CarDetail": 2, "EnableSwayTrees": 1,
            "ShadowMapType": 2, "ShadowDetail": 0, "DynamicShadowMaps": 0,
            "MirrorDetail": 0, "ParticleDetail": 1, "ShaderQuality": 2, "TwoPassTrees": 0,
            "NumDynamicCubemaps": 0, "NumFixedCubemaps": 0, "SSAO": 0, "SSRLevel": 0,
            "HeatHaze": 0, "DepthOfField": 0, "HeadlightLevel": 1, "SkyRefreshRate": 1,
            "MSAASamples": 4, "MaxAniso": 16, "DynamicTrackTextureUpdateRate": 2,
            "ParallelSorting": 1, "OcclusionCull": 1, "NvReflexMode": 1,
        },
        "lod_factor": 0.85, "cap_factor": 1.0,
    },
    "calidad": {
        "title": _("Calidad gráfica"),
        "desc": _("Máxima calidad visual asumiendo menos FPS. Para equipos sobrados, hotlaps o grabar contenido."),
        "values": {
            "MaxCarsToDraw": 40, "MaxCarsToDrawInMirrors": 10,
            "MaxPitObjsToDraw": 30, "MaxPitObjsToDrawInMirrors": 4,
            "CrowdDetail": None, "GrandstandDetail": None, "PitObjectDetail": None, "ObjectDetail": None,
            "FoliageDetail": None, "CarDetail": 2, "EnableSwayTrees": 1,
            "ShadowMapType": 3, "ShadowDetail": 1, "DynamicShadowMaps": 1,
            "MirrorDetail": 1, "ParticleDetail": 2, "ShaderQuality": 3, "TwoPassTrees": 1,
            "NumDynamicCubemaps": 1, "NumFixedCubemaps": None, "SSAO": 1, "SSRLevel": None,
            "HeatHaze": 1, "DepthOfField": 0, "HeadlightLevel": 2, "SkyRefreshRate": 2,
            "MSAASamples": 4, "MaxAniso": 16, "DynamicTrackTextureUpdateRate": 3,
            "ParallelSorting": 1, "OcclusionCull": 1, "NvReflexMode": 1,
        },
        "lod_factor": None, "cap_factor": 0.9,
    },
}

ORDER = ["rendimiento", "equilibrado", "calidad"]

# Ajustes que en iRacing cargan el hilo principal (más draw calls o cámaras extra), no solo la GPU.
CPU_KEYS = {"maxcarstodraw", "maxcarstodrawinmirrors", "maxpitobjstodraw", "maxpitobjstodrawinmirrors",
            "crowddetail", "grandstanddetail", "pitobjectdetail", "objectdetail", "foliagedetail", "cardetail",
            "shadowmaptype", "shadowdetail", "dynamicshadowmaps", "mirrordetail", "numdynamiccubemaps"}


def cpu_bound(analysis: dict | None) -> bool:
    """True si la sesión estuvo limitada por CPU/motor la mayor parte del tiempo."""
    if not analysis:
        return False
    bn = analysis["groups"][analysis["primary"]].get("bottleneck") or {}
    return bn.get("cpu", 0) + bn.get("mixed", 0) / 2 >= 50


def _round5(x):
    return int(math.floor(x / 5) * 5)


def build_profile(name: str, renderer_flat: dict[str, str], refresh: float | None = None,
                  analysis: dict | None = None, nvidia: bool = True, vr_hz: float | None = None) -> list[dict]:
    """Devuelve la lista de cambios respecto al ini actual (solo claves existentes y distintas)."""
    p = PROFILES[name]
    vals = dict(p["values"])
    if not nvidia:
        vals["NvReflexMode"] = None

    vrr_cap = int(refresh - 3) if refresh else None
    p10 = p50 = None
    if analysis:
        f = analysis["groups"][analysis["primary"]]["fps"]
        # Con Reflex (NVIDIA) la latencia ya está controlada: el tope se basa en lo que mantienes el 75 % del
        # tiempo, no el 90 %, para no renunciar a tantos FPS en zonas limpias.
        p10 = (f.get("p25") or f.get("p10")) if nvidia else f.get("p10")
        p50 = f.get("median")
    guard = name != "calidad" and cpu_bound(analysis)

    if vr_hz:
        # VR: el visor marca el ritmo (el tope de iRacing no aplica); el LOD dinámico actúa al bajar de su frecuencia
        vals["LODMinFPSTarget"] = int(vr_hz) if name != "calidad" else 60
    else:
        # Tope de FPS: base = lo que mantienes el 90 % del tiempo (si hay análisis), sin pasar de Hz-3
        if p10:
            cap = max(60, _round5(p10 * p["cap_factor"]))
            cap = min(cap, vrr_cap) if vrr_cap else cap
        else:
            cap = vrr_cap if name != "calidad" or not vrr_cap else max(60, _round5(vrr_cap * 0.6))
        if cap:
            vals["LimitFrameRate"] = 1
            vals["DesiredFPSLimit"] = cap

        # LOD dinámico: actúa por debajo de este umbral
        if p["lod_factor"] and (p50 or cap):
            vals["LODMinFPSTarget"] = max(60, _round5(min(p50 or cap, cap or p50) * p["lod_factor"]))
        elif name == "calidad":
            vals["LODMinFPSTarget"] = 60

    changes = []
    for key, v in vals.items():
        if v is None:
            continue
        cur = renderer_flat.get(key.lower())
        if cur is None:
            continue
        if guard and key.lower() in CPU_KEYS:
            try:
                if float(v) > float(cur):
                    continue  # vas limitado por CPU: el perfil no sube lo que carga el hilo principal
            except ValueError:
                pass
        try:
            same = float(cur) == float(v)
        except ValueError:
            same = str(cur) == str(v)
        if not same:
            changes.append({"file": "renderer", "key": key, "current": cur, "proposed": str(v)})
    return changes
