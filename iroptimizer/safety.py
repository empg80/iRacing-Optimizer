"""Validaciones de seguridad comunes.

Principios:
- Solo se escriben archivos .ini conocidos de iRacing dentro de la carpeta elegida (lista blanca).
- Los valores que se escriben en un .ini no pueden contener saltos de línea, ';', '=' ni corchetes
  (evita inyectar secciones o claves nuevas desde un perfil o un JSON compartido).
- Los nombres de copias de seguridad no pueden contener rutas.
- Las rutas personales (con tu usuario de Windows) se anonimizan en los archivos de sesión.
"""
from __future__ import annotations

from .i18n import _

import os
import re
from pathlib import Path

RENDERER_FILES = (
    "rendererDX11Monitor.ini",
    "rendererDX11OpenXR.ini",
    "rendererDX11OpenVR.ini",
    "rendererDX11Oculus.ini",
    "rendererDX11.ini",
)
WRITABLE_FILES = RENDERER_FILES + ("app.ini", "core.ini")

_VALUE_RE = re.compile(r"^[A-Za-z0-9 ._+\-]{0,64}$")
_STAMP_RE = re.compile(r"^[\w\-]{1,80}$", re.UNICODE)
MAX_JSON_BYTES = 2 * 1024 * 1024


class UnsafeInput(ValueError):
    """Dato externo rechazado por seguridad."""


def safe_value(v) -> str:
    s = str(v).strip()
    if not _VALUE_RE.match(s):
        raise UnsafeInput(_("Valor no permitido para un .ini: {p0!r}", p0=s[:40]))
    return s


def is_safe_value(v) -> bool:
    try:
        safe_value(v)
        return True
    except UnsafeInput:
        return False


def safe_renderer(name: str | None) -> str:
    if not name:
        return "rendererDX11Monitor.ini"
    base = os.path.basename(str(name))
    if base != str(name) or base not in RENDERER_FILES:
        raise UnsafeInput(_("Archivo gráfico no permitido: {p0!r}", p0=str(name)[:60]))
    return base


def safe_ini_target(ini_dir: Path, name: str) -> Path:
    """Ruta a un .ini permitido dentro de ini_dir (sin escapar de la carpeta)."""
    if os.path.basename(name) != name or name not in WRITABLE_FILES:
        raise UnsafeInput(_("Archivo no permitido: {p0!r}", p0=name[:60]))
    p = (Path(ini_dir) / name).resolve()
    if p.parent != Path(ini_dir).resolve():
        raise UnsafeInput(_("Ruta fuera de la carpeta de iRacing"))
    return p


def safe_stamp(name: str) -> str:
    if not _STAMP_RE.match(name or ""):
        raise UnsafeInput(_("Nombre de copia no válido: {p0!r}", p0=str(name)[:60]))
    return name


def redact_path(p: str | None) -> str | None:
    """Sustituye la carpeta de usuario por %USERPROFILE% (no publicar tu nombre de usuario)."""
    if not p:
        return p
    home = os.path.expanduser("~")
    s = str(p)
    if home and home not in ("~", "/") and s.lower().startswith(home.lower()):
        return "%USERPROFILE%" + s[len(home):]
    return s


def system_exe(name: str, *extra_dirs: str) -> str | None:
    """Ruta absoluta a una herramienta del sistema (evita ejecutar un .exe homónimo de la carpeta actual)."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for d in (os.path.join(root, "System32"), *extra_dirs):
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return cand
    return None
