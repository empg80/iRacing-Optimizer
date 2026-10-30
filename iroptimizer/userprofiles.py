"""Perfiles guardados por el usuario (p. ej. "Monitor carreras", "VR", "Grabar vídeos").

Un perfil guarda los valores de las secciones elegidas de un archivo gráfico (rendererDX11*.ini)
y, opcionalmente, el FOV de app.ini. Al aplicarlo se escriben solo las claves que existen en el
archivo destino (por si iRacing ha cambiado claves entre versiones) y siempre con copia previa.

Se guardan como JSON en Documentos\\iRacingOptimizer\\perfiles, así se pueden copiar o compartir.
"""
from __future__ import annotations

from .i18n import _

import json
import re
from datetime import datetime
from pathlib import Path

from .iniio import IniFile
from .safety import MAX_JSON_BYTES, UnsafeInput, is_safe_value, safe_renderer

GROUPS = {
    "graficos": {"label": _("Gráficos (calidad y rendimiento)"), "file": "renderer", "sections": ["Graphics Options"]},
    "pantalla": {"label": _("Pantalla y monitores (resolución, FOV físico, triples)"), "file": "renderer",
                 "sections": ["MonitorSetup", "Display"]},
    "fov": {"label": _("FOV de conducción (app.ini)"), "file": "app", "sections": ["View"],
            "keys": ["drivingCamFOV", "virtualMirrorFOV"]},
}


def profiles_dir(base: Path) -> Path:
    d = Path(base) / "perfiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\- ]", "", name, flags=re.UNICODE).strip().replace(" ", "_")
    return s or "perfil"


def save(base: Path, name: str, ini_dir: Path, renderer_file: str, groups: list[str], description: str = "") -> Path:
    ini_dir = Path(ini_dir)
    data: dict = {"renderer": {}, "app": {}}
    rend = IniFile(ini_dir / renderer_file).as_dict()
    app_path = ini_dir / "app.ini"
    app = IniFile(app_path).as_dict() if app_path.is_file() else {}
    for g in groups:
        spec = GROUPS[g]
        src = rend if spec["file"] == "renderer" else app
        for sec in spec["sections"]:
            kv = src.get(sec, {})
            if spec.get("keys"):
                kv = {k: v for k, v in kv.items() if k.lower() in {x.lower() for x in spec["keys"]}}
            if kv:
                data[spec["file"]].setdefault(sec, {}).update(kv)
    prof = {"name": name, "description": description, "created": datetime.now().isoformat(timespec="seconds"),
            "renderer_file": renderer_file, "groups": groups, "data": data, "format": 1}
    path = profiles_dir(base) / f"{_slug(name)}.json"
    path.write_text(json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_profiles(base: Path) -> list[dict]:
    out = []
    for p in sorted(profiles_dir(base).glob("*.json")):
        try:
            if p.stat().st_size > MAX_JSON_BYTES:
                continue
            d = validate(json.loads(p.read_text(encoding="utf-8")))
            d["_path"] = str(p)
            out.append(d)
        except Exception:
            continue
    return out


def changes_for(prof: dict, ini_dir: Path, renderer_file: str | None = None) -> list[dict]:
    """Diferencias entre el perfil y los .ini actuales (solo claves existentes)."""
    ini_dir = Path(ini_dir)
    target = safe_renderer(renderer_file or prof.get("renderer_file"))
    out = []
    for file_key, fname in (("renderer", target), ("app", "app.ini")):
        secs = (prof.get("data") or {}).get(file_key) or {}
        if not secs or not (ini_dir / fname).is_file():
            continue
        cur = IniFile(ini_dir / fname)
        for sec, kv in secs.items():
            if not isinstance(kv, dict):
                continue
            for k, v in kv.items():
                if not isinstance(k, str) or not is_safe_value(v):
                    continue  # valor sospechoso en el perfil: se ignora
                c = cur.get(k, sec)
                if c is not None and str(c) != str(v).strip():
                    out.append({"file": file_key, "file_name": fname, "section": sec, "key": k,
                                "current": c, "proposed": str(v)})
    return out


def delete(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def validate(d: dict) -> dict:
    """Comprueba un perfil externo y devuelve una copia limpia."""
    if not isinstance(d, dict) or not isinstance(d.get("data"), dict) or not isinstance(d.get("name"), str):
        raise UnsafeInput(_("El archivo no es un perfil de iRacing Optimizer."))
    clean = {"renderer": {}, "app": {}}
    dropped = 0
    for fk in ("renderer", "app"):
        for sec, kv in (d["data"].get(fk) or {}).items():
            if not isinstance(sec, str) or not isinstance(kv, dict):
                continue
            for k, v in kv.items():
                if isinstance(k, str) and len(k) <= 64 and k.replace("_", "").isalnum() and is_safe_value(v):
                    clean[fk].setdefault(sec[:64], {})[k] = str(v).strip()
                else:
                    dropped += 1
    try:
        rf = safe_renderer(d.get("renderer_file"))
    except UnsafeInput:
        rf = "rendererDX11Monitor.ini"
    return {"name": d["name"][:60], "description": str(d.get("description", ""))[:200],
            "created": str(d.get("created", ""))[:25], "renderer_file": rf,
            "groups": [g for g in d.get("groups", []) if g in GROUPS], "data": clean, "format": 1,
            "_dropped": dropped}


def import_file(base: Path, src: str | Path) -> Path:
    src = Path(src)
    if src.stat().st_size > MAX_JSON_BYTES:
        raise UnsafeInput(_("Archivo demasiado grande para ser un perfil."))
    d = validate(json.loads(src.read_text(encoding="utf-8")))
    d.pop("_dropped", None)
    dst = profiles_dir(base) / f"{_slug(d['name'])}.json"
    dst.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    return dst
