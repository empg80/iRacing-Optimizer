"""Aplica cambios a los .ini con copia de seguridad, y restaura."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import psutil

from .iniio import IniFile

BACKUP_DIR = "iroptimizer_backups"
BACKUP_FILES = ("rendererDX11Monitor.ini", "rendererDX11OpenXR.ini", "rendererDX11OpenVR.ini",
                "rendererDX11Oculus.ini", "rendererDX11.ini", "app.ini", "core.ini")


def iracing_running() -> bool:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("iracingsim"):
            return True
    return False


def backup(ini_dir: Path, label: str = "") -> Path:
    """Copia todos los .ini relevantes a iroptimizer_backups/<fecha>[_label]/."""
    ini_dir = Path(ini_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else "")
    bdir = ini_dir / BACKUP_DIR / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        if (ini_dir / name).is_file():
            shutil.copy2(ini_dir / name, bdir / name)
    return bdir


def apply_changes(changes: list[dict], ini_dir: Path, renderer_file: str | None,
                  dry_run: bool = False, label: str = "") -> list[str]:
    """changes: [{file: 'renderer'|'app', key, proposed, section?}]. Solo toca claves existentes."""
    if not changes:
        return ["No hay cambios que aplicar."]
    if not dry_run and iracing_running():
        raise RuntimeError("Cierra iRacing antes de aplicar: el simulador reescribe los .ini al salir.")
    files = {"renderer": renderer_file or "rendererDX11Monitor.ini", "app": "app.ini"}
    log, opened = [], {}
    for c in changes:
        fname = files[c["file"]]
        path = Path(ini_dir) / fname
        if not path.is_file():
            log.append(f"  ! {fname} no existe, se omite {c['key']}")
            continue
        ini = opened.get(fname) or opened.setdefault(fname, IniFile(path))
        before = ini.get(c["key"], c.get("section"))
        if before is None:
            log.append(f"  ! {fname}: {c['key']} no existe, se omite")
            continue
        if str(before) == str(c["proposed"]):
            continue
        ini.set(c["key"], c["proposed"], c.get("section"))
        log.append(f"  {fname}: {c['key']} {before} → {c['proposed']}")
    if dry_run:
        return ["(simulación, no se ha escrito nada)"] + log
    bdir = backup(ini_dir, label)
    for ini in opened.values():
        ini.save()
    return [f"Copia de seguridad: {bdir}"] + log


def apply(recs_path: Path, ini_dir: Path, max_priority: int = 2, only_keys: list[str] | None = None,
          dry_run: bool = False) -> list[str]:
    data = json.loads(Path(recs_path).read_text(encoding="utf-8"))
    recs = [r for r in data["recommendations"] if r.get("key") and r["priority"] <= max_priority]
    if only_keys:
        wanted = {k.lower() for k in only_keys}
        recs = [r for r in recs if r["key"].lower() in wanted]
    return apply_changes(recs, ini_dir, data.get("renderer_file"), dry_run, "recomendaciones")


def list_backups(ini_dir: Path) -> list[Path]:
    root = Path(ini_dir) / BACKUP_DIR
    return sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.is_dir() else []


def restore(ini_dir: Path, stamp: str | None = None) -> list[str]:
    dirs = list_backups(ini_dir)
    if not dirs:
        raise FileNotFoundError("No hay copias de seguridad.")
    src = (Path(ini_dir) / BACKUP_DIR / stamp) if stamp else dirs[0]
    if not src.is_dir():
        raise FileNotFoundError(f"No existe la copia {stamp}")
    if iracing_running():
        raise RuntimeError("Cierra iRacing antes de restaurar.")
    out = []
    for f in src.iterdir():
        shutil.copy2(f, Path(ini_dir) / f.name)
        out.append(f"Restaurado {f.name} desde {src.name}")
    return out
