"""Aplica cambios a los .ini con copia de seguridad, y restaura."""
from __future__ import annotations

from .i18n import _

import json
import shutil
from datetime import datetime
from pathlib import Path

import psutil

from .iniio import IniFile
from .safety import UnsafeInput, WRITABLE_FILES, safe_ini_target, safe_renderer, safe_stamp

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
    import re
    ini_dir = Path(ini_dir)
    label = re.sub(r"[^\w\-]", "_", label, flags=re.UNICODE)[:40]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else "")
    bdir = ini_dir / BACKUP_DIR / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        if (ini_dir / name).is_file():
            shutil.copy2(ini_dir / name, bdir / name)
    return bdir


def apply_changes(changes: list[dict], ini_dir: Path, renderer_file: str | None,
                  dry_run: bool = False, label: str = "", state_base: Path | None = None) -> list[str]:
    """changes: [{file: 'renderer'|'app', key, proposed, section?}]. Solo toca claves existentes."""
    if not changes:
        return [_("No hay cambios que aplicar.")]
    if not dry_run and iracing_running():
        raise RuntimeError(_("Cierra iRacing antes de aplicar: el simulador reescribe los .ini al salir."))
    files = {"renderer": safe_renderer(renderer_file), "app": "app.ini"}
    log, opened, applied = [], {}, []
    for c in changes:
        if c.get("file") not in files:
            log.append(_("  ! tipo de archivo no permitido: {p0}", p0=str(c.get('file'))[:20]))
            continue
        fname = files[c["file"]]
        path = safe_ini_target(ini_dir, fname)
        if not path.is_file():
            log.append(_("  ! {fname} no existe, se omite {p1}", fname=fname, p1=c['key']))
            continue
        ini = opened.get(fname) or opened.setdefault(fname, IniFile(path))
        before = ini.get(c["key"], c.get("section"))
        if before is None:
            log.append(_("  ! {fname}: {p1} no existe, se omite", fname=fname, p1=c['key']))
            continue
        if str(before) == str(c["proposed"]):
            applied.append(c)
            continue
        try:
            ini.set(c["key"], c["proposed"], c.get("section"))
        except UnsafeInput as e:
            log.append(f"  ! {fname}: {c['key']} rechazado ({e})")
            continue
        applied.append(c)
        log.append(f"  {fname}: {c['key']} {before} → {c['proposed']}")
    if dry_run:
        return [_("(simulación, no se ha escrito nada)")] + log
    bdir = backup(ini_dir, label)
    for ini in opened.values():
        ini.save()
    if state_base is not None:  # recordar lo aplicado para detectar cambios posteriores
        from . import drift
        drift.record(state_base, applied, renderer_file)
    return [_("Copia de seguridad: {bdir}", bdir=bdir)] + log


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
        raise FileNotFoundError(_("No hay copias de seguridad."))
    src = (Path(ini_dir) / BACKUP_DIR / safe_stamp(stamp)) if stamp else dirs[0]
    if not src.is_dir():
        raise FileNotFoundError(_("No existe la copia {stamp}", stamp=stamp))
    if iracing_running():
        raise RuntimeError(_("Cierra iRacing antes de restaurar."))
    out = []
    for f in src.iterdir():
        if f.name not in WRITABLE_FILES or not f.is_file() or f.is_symlink():
            continue  # solo restauramos .ini conocidos de iRacing
        shutil.copy2(f, safe_ini_target(ini_dir, f.name))
        out.append(f"Restaurado {f.name} desde {src.name}")
    return out
