"""Traducción de la interfaz (español / inglés).

Los textos del código están en español y se envuelven con `_()`. En inglés se buscan en el catálogo
`locale_en.CATALOG` (clave = plantilla en español). Si falta una traducción se muestra el español.

Idioma: variable de entorno IROPT_LANG, o `settings.json` en Documentos\\iRacingOptimizer, o el
idioma de Windows (español → es; cualquier otro → en). Cambiarlo requiere reiniciar la app.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SUPPORTED = ("es", "en")


def _settings_path() -> Path:
    try:
        from .config_reader import documents_dir
        return documents_dir() / "iRacingOptimizer" / "settings.json"
    except Exception:
        return Path.home() / "Documents" / "iRacingOptimizer" / "settings.json"


def _system_lang() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "es" if (lid & 0x3FF) == 0x0A else "en"
        except Exception:
            pass
    import locale
    loc = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
    return "es" if loc.startswith("es") else "en"


def _detect() -> str:
    env = os.environ.get("IROPT_LANG", "").lower()
    if env in SUPPORTED:
        return env
    try:
        d = json.loads(_settings_path().read_text(encoding="utf-8"))
        if d.get("lang") in SUPPORTED:
            return d["lang"]
    except Exception:
        pass
    return _system_lang()


LANG = _detect()
_CATALOG: dict[str, str] | None = None


def _catalog() -> dict[str, str]:
    global _CATALOG
    if _CATALOG is None:
        from .locale_en import CATALOG
        _CATALOG = CATALOG
    return _CATALOG


def _(template: str, **kw) -> str:
    s = _catalog().get(template, template) if LANG == "en" else template
    try:
        return s.format(**kw)
    except (KeyError, IndexError, ValueError):
        return template.format(**kw)


def load_settings() -> dict:
    """Ajustes de la app (settings.json). Vacío si no existe o está dañado."""
    try:
        d = json.loads(_settings_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_setting(key: str, value) -> None:
    p = _settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        d = load_settings()
        d[key] = value
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError:
        pass


def set_lang(lang: str) -> None:
    """Guarda el idioma elegido (se aplica al reiniciar)."""
    if lang not in SUPPORTED:
        return
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:
        d = {}
    d["lang"] = lang
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")
