"""Inventario del equipo: CPU, RAM, GPU, pantalla y ajustes de Windows relevantes.

Todo es best-effort: si algo no se puede leer se devuelve None y el análisis lo ignora.
"""
from __future__ import annotations

from .i18n import _

import platform
import re
import subprocess
import sys

import psutil

from .safety import system_exe

POWER_PLANS = {
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "Alto rendimiento",
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Equilibrado",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Economizador",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": _("Máximo rendimiento"),
}


def _reg(root, path, name):
    try:
        import winreg
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def cpu_info() -> dict:
    name = None
    if sys.platform == "win32":
        import winreg
        name = _reg(winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
    if not name:
        try:
            with open("/proc/cpuinfo") as f:
                m = re.search(r"model name\s*:\s*(.+)", f.read())
                name = m.group(1) if m else None
        except Exception:
            name = platform.processor() or None
    return {
        "name": (name or "desconocida").strip(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "x3d": bool(name and "X3D" in name.upper()),
    }


def gpu_info() -> list[dict]:
    gpus = []
    try:
        import pynvml
        pynvml.nvmlInit()
        drv = pynvml.nvmlSystemGetDriverVersion()
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            gpus.append({
                "name": name.decode() if isinstance(name, bytes) else name,
                "vram_mb": pynvml.nvmlDeviceGetMemoryInfo(h).total // (1024 * 1024),
                "driver": drv.decode() if isinstance(drv, bytes) else drv,
                "vendor": "nvidia",
            })
        pynvml.nvmlShutdown()
        return gpus
    except Exception:
        pass
    exe = system_exe("nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI") if sys.platform == "win32" else None
    try:  # sin pynvml pero con driver NVIDIA (ruta absoluta: nunca un nvidia-smi de la carpeta actual)
        if not exe:
            raise FileNotFoundError
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
        for line in out.strip().splitlines():
            n, mem, drv = [x.strip() for x in line.split(",")]
            gpus.append({"name": n, "vram_mb": int(float(mem)), "driver": drv, "vendor": "nvidia"})
    except Exception:
        pass
    return gpus


def display_info() -> dict:
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
            ]

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        ENUM_CURRENT_SETTINGS = wintypes.DWORD(0xFFFFFFFF)
        if ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            return {"width": dm.dmPelsWidth, "height": dm.dmPelsHeight, "refresh": dm.dmDisplayFrequency}
    except Exception:
        pass
    return {}


def windows_tweaks() -> dict:
    if sys.platform != "win32":
        return {}
    import winreg
    out: dict = {}
    try:
        exe = system_exe("powercfg.exe")
        if not exe:
            raise FileNotFoundError("powercfg")
        txt = subprocess.run([exe, "/getactivescheme"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", txt, re.I)
        if m:
            g = m.group(1).lower()
            out["power_plan"] = {"guid": g, "name": POWER_PLANS.get(g, "Personalizado")}
    except Exception:
        pass
    hags = _reg(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
    out["hags"] = None if hags is None else hags == 2
    gm = _reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\GameBar", "AutoGameModeEnabled")
    out["game_mode"] = None if gm is None else bool(gm)
    return out


def get_hardware() -> dict:
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": cpu_info(),
        "ram_mb": psutil.virtual_memory().total // (1024 * 1024),
        "gpus": gpu_info(),
        "display": display_info(),
        "windows": windows_tweaks(),
    }
