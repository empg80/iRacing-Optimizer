"""Detección de cambios en la configuración desde la última vez que aplicaste ajustes.

Cada vez que la app aplica cambios, guarda los valores escritos como "referencia". Al abrirla,
compara la referencia con los .ini actuales. Si difieren, es que iRacing (por una actualización o
porque reseteó la configuración) o tú desde el simulador los cambiasteis, y ofrece volver a aplicar
o aceptar los nuevos como referencia.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .iniio import IniFile
from .safety import MAX_JSON_BYTES, WRITABLE_FILES, is_safe_value, safe_renderer

STATE_FILE = "referencia_aplicada.json"


def _path(base: Path) -> Path:
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base) / STATE_FILE


def load(base: Path) -> dict:
    p = _path(base)
    if not p.is_file() or p.stat().st_size > MAX_JSON_BYTES:
        return {"files": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d.get("files"), dict) else {"files": {}}
    except (OSError, ValueError):
        return {"files": {}}


def save(base: Path, state: dict) -> None:
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    _path(base).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def record(base: Path, changes: list[dict], renderer_file: str | None) -> None:
    """Añade a la referencia los valores recién aplicados (el último gana)."""
    st = load(base)
    files = {"renderer": safe_renderer(renderer_file), "app": "app.ini"}
    for c in changes:
        fname = files.get(c.get("file"))
        if not fname or not is_safe_value(c.get("proposed", "")):
            continue
        sec = c.get("section") or ""
        st["files"].setdefault(fname, {}).setdefault(sec, {})[c["key"]] = str(c["proposed"]).strip()
    save(base, st)


def clear(base: Path) -> None:
    p = _path(base)
    if p.exists():
        p.unlink()


def check(base: Path, ini_dir: Path) -> list[dict]:
    """Diferencias entre la referencia y los .ini actuales."""
    st = load(base)
    out = []
    for fname, secs in st.get("files", {}).items():
        if fname not in WRITABLE_FILES or not (Path(ini_dir) / fname).is_file():
            continue
        ini = IniFile(Path(ini_dir) / fname)
        for sec, kv in secs.items():
            for k, expected in kv.items():
                cur = ini.get(k, sec or None)
                if cur is None:
                    out.append({"file_name": fname, "section": sec, "key": k, "expected": expected, "current": "(no existe)"})
                    continue
                try:
                    same = abs(float(cur) - float(expected)) < 1e-6
                except ValueError:
                    same = str(cur) == str(expected)
                if not same:
                    out.append({"file_name": fname, "section": sec, "key": k, "expected": expected, "current": cur})
    return out


def accept_current(base: Path, ini_dir: Path) -> None:
    """Toma los valores actuales como nueva referencia (para las mismas claves)."""
    st = load(base)
    for fname, secs in st.get("files", {}).items():
        if fname not in WRITABLE_FILES or not (Path(ini_dir) / fname).is_file():
            continue
        ini = IniFile(Path(ini_dir) / fname)
        for sec, kv in secs.items():
            for k in list(kv):
                cur = ini.get(k, sec or None)
                if cur is None:
                    del kv[k]
                else:
                    kv[k] = cur
    save(base, st)


def reapply_changes(diffs: list[dict]) -> dict[str, list[dict]]:
    """Agrupa por archivo los cambios para volver a aplicar la referencia."""
    by: dict[str, list[dict]] = {}
    for d in diffs:
        if d["current"] == "(no existe)":
            continue
        kind = "app" if d["file_name"] == "app.ini" else "renderer"
        by.setdefault(d["file_name"], []).append({"file": kind, "section": d["section"] or None, "key": d["key"],
                                                  "current": d["current"], "proposed": d["expected"]})
    return by
