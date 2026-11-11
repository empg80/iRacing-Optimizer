"""Datos del sistema para el chequeo previo: BIOS, RAM, enlace PCIe/ReBAR, pantallas y ajustes de Windows.

Todo se lee sin administrador y sin programas externos:
- BIOS, placa y RAM: tabla SMBIOS con GetSystemFirmwareTable("RSMB") (la misma que lee el Administrador de tareas).
- Resizable BAR y PCIe: NVML del driver NVIDIA.
- Pantallas: EnumDisplayDevices / EnumDisplaySettings.
- Ajustes de Windows: registro del usuario.

Best-effort como hardware.py: lo que no se pueda leer queda en None y el chequeo lo ignora.
"""
from __future__ import annotations

import struct
import sys

MEM_TYPES = {0x18: "DDR3", 0x1A: "DDR4", 0x1E: "LPDDR4", 0x22: "DDR5", 0x23: "LPDDR5"}


# ---------------------------------------------------------------------------- SMBIOS

def _strings(data: bytes, start: int) -> tuple[list[str], int]:
    """Cadenas que siguen a la parte formateada de una estructura; devuelve (cadenas, inicio de la siguiente)."""
    end = data.find(b"\x00\x00", start)
    if end < 0:
        return [], len(data)
    raw = data[start:end]
    out = [s.decode("latin-1", "replace").strip() for s in raw.split(b"\x00")] if raw else []
    return out, end + 2


def parse_smbios(table: bytes) -> dict:
    """Analiza la tabla SMBIOS (sin la cabecera RawSMBIOSData). Devuelve bios, placa y módulos de RAM."""
    out: dict = {"bios": {}, "board": {}, "memory": []}
    i = 0
    while i + 4 <= len(table):
        typ, length = table[i], table[i + 1]
        if length < 4 or i + length > len(table):
            break
        f = table[i:i + length]
        strs, nxt = _strings(table, i + length)

        def s(off):
            if off >= length or not f[off] or f[off] > len(strs):
                return None
            return strs[f[off] - 1] or None

        def w(off):
            return struct.unpack_from("<H", f, off)[0] if off + 2 <= length else None

        def dw(off):
            return struct.unpack_from("<I", f, off)[0] if off + 4 <= length else None

        if typ == 0:
            out["bios"] = {"vendor": s(4), "version": s(5), "date": s(8)}
        elif typ == 2:
            out["board"] = {"vendor": s(4), "product": s(5)}
        elif typ == 17:
            size = w(0x0C)
            if size and size != 0xFFFF:
                size_mb = (dw(0x1C) or 0) if size == 0x7FFF else (size & 0x7FFF) / (1024 if size & 0x8000 else 1)
                speed, conf = w(0x15), w(0x20)
                if speed == 0xFFFF:
                    speed = dw(0x54)
                if conf == 0xFFFF:
                    conf = dw(0x58)
                out["memory"].append({
                    "size_mb": int(size_mb),
                    "type": MEM_TYPES.get(f[0x12]) if length > 0x12 else None,
                    "speed": speed or None,         # máxima declarada por el módulo (MT/s)
                    "configured": conf or None,     # a la que funciona ahora (MT/s)
                    "part": s(0x1A),
                })
        elif typ == 127:
            break
        i = nxt
    return out


def smbios() -> dict:
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        sig = struct.unpack(">I", b"RSMB")[0]
        n = k32.GetSystemFirmwareTable(sig, 0, None, 0)
        if not n:
            return {}
        buf = ctypes.create_string_buffer(n)
        if k32.GetSystemFirmwareTable(sig, 0, buf, n) != n:
            return {}
        raw = buf.raw
        length = struct.unpack_from("<I", raw, 4)[0]
        return parse_smbios(raw[8:8 + length])
    except Exception:
        return {}


# ---------------------------------------------------------------------------- GPU (NVML)

def gpu_links() -> list[dict]:
    out = []
    try:
        import pynvml
        pynvml.nvmlInit()
    except Exception:
        return out
    try:
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            d: dict = {}
            for key, fn in (("bar1_mb", lambda: pynvml.nvmlDeviceGetBAR1MemoryInfo(h).bar1Total // (1024 * 1024)),
                            ("width", lambda: pynvml.nvmlDeviceGetCurrPcieLinkWidth(h)),
                            ("width_max", lambda: pynvml.nvmlDeviceGetMaxPcieLinkWidth(h)),
                            ("gen_max", lambda: pynvml.nvmlDeviceGetMaxPcieLinkGeneration(h)),
                            ("gen_gpu", lambda: pynvml.nvmlDeviceGetGpuMaxPcieLinkGeneration(h))):
                try:
                    d[key] = int(fn())
                except Exception:
                    d[key] = None
            out.append(d)
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------- pantallas

def displays() -> list[dict]:
    """Pantallas activas: gráfica a la que van, si es la principal, modo actual y máxima frecuencia a esa resolución."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        from .hardware import _devmode_class
        DEVMODEW = _devmode_class()

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                        ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

        u32 = ctypes.windll.user32
        out, i = [], 0
        while i < 16:
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(dd)
            if not u32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            i += 1
            if not dd.StateFlags & 0x1:  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                continue
            cur = DEVMODEW()
            cur.dmSize = ctypes.sizeof(cur)
            if not u32.EnumDisplaySettingsW(dd.DeviceName, wintypes.DWORD(0xFFFFFFFF), ctypes.byref(cur)):
                continue
            best, m = cur.dmDisplayFrequency, 0
            while m < 2000:
                dm = DEVMODEW()
                dm.dmSize = ctypes.sizeof(dm)
                if not u32.EnumDisplaySettingsW(dd.DeviceName, m, ctypes.byref(dm)):
                    break
                m += 1
                if dm.dmPelsWidth == cur.dmPelsWidth and dm.dmPelsHeight == cur.dmPelsHeight:
                    best = max(best, dm.dmDisplayFrequency)
            out.append({"name": dd.DeviceName, "adapter": dd.DeviceString, "primary": bool(dd.StateFlags & 0x4),
                        "width": cur.dmPelsWidth, "height": cur.dmPelsHeight,
                        "refresh": cur.dmDisplayFrequency, "refresh_max": best})
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------- Windows

def windows_extra() -> dict:
    if sys.platform != "win32":
        return {}
    import platform
    import winreg
    from .hardware import _reg
    out: dict = {}
    try:
        out["build"] = int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        out["build"] = None
    g = _reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX\UserGpuPreferences", "DirectXUserGlobalSettings")
    if isinstance(g, str) and "SwapEffectUpgradeEnable=" in g:
        out["windowed_opt"] = "SwapEffectUpgradeEnable=1" in g
    else:
        out["windowed_opt"] = None  # sin tocar: valor por defecto de Windows
    hist = _reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "HistoricalCaptureEnabled")
    out["background_recording"] = None if hist is None else bool(hist)
    return out


def bios_from_registry() -> dict:
    """Alternativa a SMBIOS para BIOS y placa: la copia que Windows guarda en el registro."""
    if sys.platform != "win32":
        return {}
    import winreg
    from .hardware import _reg
    key = r"HARDWARE\DESCRIPTION\System\BIOS"
    g = lambda n: _reg(winreg.HKEY_LOCAL_MACHINE, key, n)  # noqa: E731
    return {"bios": {"vendor": g("BIOSVendor"), "version": g("BIOSVersion"), "date": g("BIOSReleaseDate")},
            "board": {"vendor": g("BaseBoardManufacturer"), "product": g("BaseBoardProduct")}}


def collect() -> dict:
    d = smbios()
    if not (d.get("bios") or {}).get("version"):
        d.update(bios_from_registry())
    return {**d, "gpu_links": gpu_links(), "displays": displays(), "windows": windows_extra()}
