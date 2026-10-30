"""Localiza Documentos\\iRacing y carga renderer*.ini, app.ini y core.ini."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .iniio import IniFile

from .safety import RENDERER_FILES, redact_path, safe_renderer


def documents_dir() -> Path:
    """Carpeta Documentos real (respeta la redirección a OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            g = GUID(u.fields[0], u.fields[1], u.fields[2],
                     (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = Path(p.value)
                ctypes.windll.ole32.CoTaskMemFree(p)
                return path
        except Exception:
            pass
    return Path(os.path.expanduser("~")) / "Documents"


def find_iracing_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("IRACING_DIR")
    if env:
        return Path(env)
    return documents_dir() / "iRacing"


@dataclass
class IRacingConfig:
    directory: Path
    renderer: IniFile | None = None
    app: IniFile | None = None
    core: IniFile | None = None
    renderer_candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def renderer_name(self) -> str | None:
        return self.renderer.path.name if self.renderer else None


def load(ini_dir: str | None = None, renderer: str | None = None) -> IRacingConfig:
    d = find_iracing_dir(ini_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"No encuentro la carpeta de iRacing: {d}  (usa --ini-dir)")
    cfg = IRacingConfig(directory=d)

    cands = [(n, (d / n).stat().st_mtime) for n in RENDERER_FILES if (d / n).is_file()]
    cfg.renderer_candidates = sorted(cands, key=lambda x: -x[1])
    if renderer:
        cfg.renderer = IniFile(d / safe_renderer(renderer))
    else:
        # Prioridad: Monitor si existe (pantalla plana); si no, el modificado más reciente.
        names = [n for n, _ in cfg.renderer_candidates]
        pick = "rendererDX11Monitor.ini" if "rendererDX11Monitor.ini" in names else (names[0] if names else None)
        if pick:
            cfg.renderer = IniFile(d / pick)

    for attr, name in (("app", "app.ini"), ("core", "core.ini")):
        if (d / name).is_file():
            setattr(cfg, attr, IniFile(d / name))
    return cfg


def snapshot(cfg: IRacingConfig) -> dict:
    return {
        "directory": redact_path(str(cfg.directory)),  # sin tu nombre de usuario si compartes la sesión
        "renderer_file": cfg.renderer_name,
        "renderer_candidates": cfg.renderer_candidates,
        "renderer": cfg.renderer.as_dict() if cfg.renderer else {},
        "app": cfg.app.as_dict() if cfg.app else {},
        "core": cfg.core.as_dict() if cfg.core else {},
    }


def flat_from_snapshot(section_dict: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for sec in section_dict.values():
        for k, v in sec.items():
            out.setdefault(k.lower(), v)
    return out
