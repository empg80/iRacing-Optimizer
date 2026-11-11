"""Exportar una sesión anonimizada (.zip) para compartirla e importar las que te envíen.

Qué se quita al exportar:
- Nombres de procesos en segundo plano (solo se deja el % total de CPU de fondo).
- Rutas de carpetas y cualquier sección de app.ini/core.ini que no sea necesaria para el análisis
  (dispositivos de audio, mensajes de chat, perfiles de controles, coches con setup guardado…).
- Del archivo gráfico solo se dejan las secciones que usa el análisis.

Al importar, el .zip se trata como dato no fiable: solo se aceptan los tres archivos esperados,
sin rutas, con límites de tamaño y sin descomprimir nada más.
"""
from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from . import presentmon
from .i18n import _

RENDERER_SECTIONS = ("Graphics Options", "MonitorSetup", "Display")
APP_KEEP = {"Graphics": ("serverTransmitMaxCars", "maxCars"), "View": ("drivingCamFOV",),
            "Misc": ("irsdkEnableDisk", "irsdkAutoLogDisk", "irsdkLogAllCars")}
DISPLAY_DROP = ("deviceIdx",)
MAX_CSV = 200 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
NAME_RE = re.compile(r"^session_[\w\-]{1,60}$")


def anonymize_meta(meta: dict) -> dict:
    m = json.loads(json.dumps(meta))  # copia profunda
    cfg = m.get("config") or {}
    rend = cfg.get("renderer") or {}
    cfg["renderer"] = {s: {k: v for k, v in rend.get(s, {}).items() if k not in DISPLAY_DROP}
                       for s in RENDERER_SECTIONS if s in rend}
    app = cfg.get("app") or {}
    cfg["app"] = {sec: {k: app[sec][k] for k in keys if k in app.get(sec, {})}
                  for sec, keys in APP_KEEP.items() if sec in app}
    cfg["app"] = {k: v for k, v in cfg["app"].items() if v}
    cfg["core"] = {}
    cfg["directory"] = None
    cfg["renderer_candidates"] = [[n, 0] for n, _t in cfg.get("renderer_candidates", [])]
    m["config"] = cfg
    tops = m.pop("top_processes", None) or []
    m["background_cpu_pct"] = round(sum(p.get("cpu_pct", 0) for p in tops), 1) if tops else None
    hw = m.get("hardware") or {}
    hw.pop("os", None)
    m["hardware"] = hw
    m["anonymized"] = True
    m["exported"] = datetime.now().isoformat(timespec="seconds")
    return m


def export_session(csv_path: str | Path, out_zip: str | Path | None = None) -> Path:
    csv_path = Path(csv_path)
    meta_path = csv_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    stem = csv_path.stem
    out_zip = Path(out_zip) if out_zip else csv_path.with_name(f"{stem}_compartir.zip")
    anon = anonymize_meta(meta)
    pm_name = (meta.get("presentmon") or {}).get("csv")
    pm_path = presentmon.find_file(csv_path.parent / Path(pm_name).name) if pm_name else None
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(csv_path, f"{stem}.csv")
        if pm_path:
            pm_arc = f"{stem}.presentmon.csv" + (".gz" if pm_path.suffix == ".gz" else "")
            # un .gz ya va comprimido: se guarda sin volver a comprimir
            z.write(pm_path, pm_arc, compress_type=zipfile.ZIP_STORED if pm_path.suffix == ".gz" else None)
            anon["presentmon"] = {k: v for k, v in (anon.get("presentmon") or {}).items() if k in ("t0",)}
            anon["presentmon"]["csv"] = pm_arc
        z.writestr(f"{stem}.json", json.dumps(anon, indent=2, ensure_ascii=False))
    return out_zip


def import_session(zip_path: str | Path, dest_dir: str | Path) -> Path:
    """Extrae una sesión compartida de forma segura. Devuelve la ruta del CSV de telemetría."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        csvs = [n for n in names if n.endswith(".csv") and not n.endswith(".presentmon.csv")]
        if len(csvs) != 1:
            raise ValueError(_("El archivo no parece una sesión de iRacing Optimizer."))
        stem = Path(csvs[0]).stem
        if "/" in csvs[0] or "\\" in csvs[0] or not NAME_RE.match(stem):
            raise ValueError(_("El archivo no parece una sesión de iRacing Optimizer."))
        allowed = {f"{stem}.csv": MAX_CSV, f"{stem}.json": MAX_JSON, f"{stem}.presentmon.csv": MAX_CSV,
                   f"{stem}.presentmon.csv.gz": MAX_CSV}
        # renombrar para no pisar sesiones propias
        new_stem = f"{stem}_recibida"
        out_csv = None
        for info in z.infolist():
            if info.filename not in allowed:
                continue  # se ignora cualquier otro contenido
            if info.file_size > allowed[info.filename]:
                raise ValueError(_("Archivo demasiado grande dentro del .zip."))
            data = z.read(info.filename)
            suffix = info.filename[len(stem):]
            target = dest_dir / f"{new_stem}{suffix}"
            if suffix == ".json":
                meta = json.loads(data.decode("utf-8"))
                if not isinstance(meta, dict):
                    raise ValueError(_("El archivo no parece una sesión de iRacing Optimizer."))
                pm = meta.get("presentmon")
                if isinstance(pm, dict) and pm.get("csv"):
                    gz = str(pm["csv"]).endswith(".gz")
                    pm["csv"] = f"{new_stem}.presentmon.csv" + (".gz" if gz else "")
                meta["received"] = True
                data = json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8")
            target.write_bytes(data)
            if suffix == ".csv":
                out_csv = target
    if not out_csv:
        raise ValueError(_("El archivo no parece una sesión de iRacing Optimizer."))
    return out_csv
