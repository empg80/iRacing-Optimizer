"""Soporte de realidad virtual: detección del runtime y análisis de FPS frente a la frecuencia del visor.

En VR el objetivo no es el tope de FPS sino la frecuencia del visor (p. ej. 90 Hz en Quest 3). Si el
simulador no llega, el runtime activa reproyección (ASW en Meta, Motion Smoothing en SteamVR,
SSW en Virtual Desktop): el juego pasa a renderizar a la mitad de Hz y se inventan los frames
intermedios, lo que se ve como "fantasmas" en coches y bordes. Eso se detecta en la telemetría
como FPS pegados a ~Hz/2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psutil

from .i18n import _

HEADSETS = {
    "Meta Quest 3 / 3S": [72, 80, 90, 120],
    "Meta Quest 2": [72, 80, 90, 120],
    "Meta Quest Pro": [72, 90],
    "Valve Index": [80, 90, 120, 144],
    "Pico 4": [72, 90],
    "Otro": [60, 72, 75, 80, 90, 120, 144],
}
VR_RENDERERS = ("rendererDX11OpenXR.ini", "rendererDX11OpenVR.ini", "rendererDX11Oculus.ini")

PROCESSES = {
    "ovrserver_x64.exe": "Meta Quest Link",
    "virtualdesktop.streamer.exe": "Virtual Desktop",
    "virtualdesktop.service.exe": "Virtual Desktop",
    "vrserver.exe": "SteamVR",
    "vrcompositor.exe": "SteamVR",
    "alvr_dashboard.exe": "ALVR",
}
RUNTIME_HINTS = {
    "oculus": "Meta Quest Link",
    "virtualdesktop": "Virtual Desktop (VDXR)",
    "steamxr": "SteamVR",
    "steamvr": "SteamVR",
    "pimax": "Pimax",
    "wmr": "Windows Mixed Reality",
}


def active_openxr_runtime() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\OpenXR\1") as k:
            path = winreg.QueryValueEx(k, "ActiveRuntime")[0]
    except Exception:
        return None
    low = Path(str(path)).name.lower()
    for key, name in RUNTIME_HINTS.items():
        if key in low:
            return name
    try:  # el JSON del runtime suele llevar su nombre
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return (d.get("runtime") or {}).get("name") or low
    except Exception:
        return low


def running_vr_software() -> list[str]:
    found = []
    for p in psutil.process_iter(["name"]):
        n = (p.info["name"] or "").lower()
        if n in PROCESSES and PROCESSES[n] not in found:
            found.append(PROCESSES[n])
    return found


def detect() -> dict:
    """Software de VR abierto. OJO: que esté abierto no significa que estés en VR (el servicio de Meta,
    OVRServer_x64.exe, corre siempre en segundo plano). Por eso `active` ya no se deduce de aquí."""
    procs = running_vr_software()
    return {"runtime": active_openxr_runtime(), "software": procs, "active": False}


# Bibliotecas que iRacing solo carga cuando renderiza en VR
VR_MODULES = ("openxr_loader", "libovrrt", "openvr_api")


def iracing_in_vr(proc) -> bool | None:
    """True/False según si el proceso de iRacing tiene cargado un runtime de VR; None si no se puede saber."""
    if proc is None:
        return None
    try:
        for m in proc.memory_maps(grouped=True):
            name = Path(getattr(m, "path", "") or "").name.lower()
            if any(name.startswith(x) for x in VR_MODULES):
                return True
        return False
    except Exception:
        return None


def is_vr_session(meta: dict) -> bool:
    check = meta.get("vr_check")
    if check is not None:  # comprobado durante la grabación: manda sobre la casilla y el archivo gráfico
        return bool(check)
    v = meta.get("vr") or {}
    if v.get("active"):
        return True
    rf = ((meta.get("config") or {}).get("renderer_file") or "")
    return rf in VR_RENDERERS


def detect_rate(fps: list[float], chosen_hz: float, headset: str | None = None) -> float | None:
    """Frecuencia real del visor si los FPS se quedan clavados en otra distinta de la elegida.

    Con el juego al ritmo del visor los FPS forman una meseta en su frecuencia (p. ej. 72 aunque se eligiera 90).
    Solo se consideran frecuencias de ese visor por debajo de la elegida y nunca ~la mitad (eso es reproyección).
    """
    vals = [f for f in fps if f and f > 1]
    if len(vals) < 40 or not chosen_hz:
        return None
    rates = HEADSETS.get(headset or "", HEADSETS["Otro"])
    for r in sorted(rates, reverse=True):
        if r >= chosen_hz * 0.95 or abs(r - chosen_hz / 2) <= 2:
            continue
        near = sum(1 for f in vals if abs(f - r) <= 1.5)
        above = sum(1 for f in vals if f > r + 1.5)
        if near / len(vals) >= 0.6 and above / len(vals) < 0.02:
            return float(r)
    return None


def analyze(rows: list[dict], hz: float) -> dict:
    """Reparto del tiempo: a la frecuencia del visor, en reproyección (~Hz/2) o por debajo."""
    fps = [r["fps"] for r in rows if r.get("fps")]
    if not fps or not hz:
        return {}
    n = len(fps)
    at = sum(1 for f in fps if f >= hz * 0.95)
    half = sum(1 for f in fps if hz * 0.44 <= f <= hz * 0.56)
    below = n - at - half
    # periodos de reproyección (>= 2 s seguidos en ~Hz/2)
    periods, cur = 0, 0
    for f in fps:
        if hz * 0.44 <= f <= hz * 0.56:
            cur += 1
        else:
            if cur >= 8:
                periods += 1
            cur = 0
    if cur >= 8:
        periods += 1
    return {"hz": hz, "at_rate_pct": 100 * at / n, "reprojection_pct": 100 * half / n,
            "below_pct": 100 * below / n, "reprojection_periods": periods}


def runtime_tips(runtime: str | None, software: list[str]) -> list[str]:
    name = (runtime or "") + " " + " ".join(software)
    tips = []
    if "Virtual Desktop" in name:
        tips += [_("Virtual Desktop: usa su runtime VDXR (no SteamVR) para que iRacing vaya directo por OpenXR."),
                 _("Virtual Desktop: la codificación usa GPU; si vas justo, baja el bitrate o la calidad (High en vez de Ultra) antes que los gráficos."),
                 _("Inalámbrico: router Wi-Fi 6/6E dedicado y cerca, PC por cable al router.")]
    if "Meta Quest Link" in name:
        tips += [_("Meta Horizon Link (antes Meta Quest Link): ajusta frecuencia y resolución en la app del PC → Dispositivos → Gráficos. Baja resolución antes que subir detalles."),
                 _("Con cable USB 3 suele ser más estable que Air Link; con Air Link usa Wi-Fi 6/6E dedicado.")]
    if "SteamVR" in name:
        tips.append(_("SteamVR añade una capa más con Quest. Si usas Quest, mejor Meta Quest Link o Virtual Desktop (VDXR)."))
    if not tips:
        tips.append(_("No he detectado el software de VR mientras grababas. Graba con el visor conectado para ver consejos de tu runtime."))
    return tips
