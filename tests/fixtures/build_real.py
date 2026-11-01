"""Genera las sesiones reales de regresión (tests/fixtures/real) a partir de sesiones grabadas.

Uso: python tests/fixtures/build_real.py <carpeta_con_sesiones> nombre1 [nombre2 ...]
Cada nombre es el stem de una sesión (session_YYYYMMDD_HHMMSS) con su .csv, .json y, opcional, .presentmon.csv.

Privacidad: el JSON pasa por sharing.anonymize_meta (sin rutas, audio, controles ni SO). Solo se conservan los
nombres de los programas en segundo plano, que el test de carga de fondo necesita. PresentMon se reduce a las 5
columnas que usa la app y se redondea a 3 decimales. Todo se guarda comprimido (.gz).
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from iroptimizer import sharing  # noqa: E402

OUT = Path(__file__).resolve().parent / "real"
PM_COLS = ("Application", "CPUStartDateTime", "MsBetweenPresents", "MsCPUBusy", "MsGPUBusy")


def build(src: Path, stem: str) -> None:
    meta = json.loads((src / f"{stem}.json").read_text(encoding="utf-8"))
    anon = sharing.anonymize_meta(meta)
    anon["top_processes"] = meta.get("top_processes") or []  # nombres de programas genéricos (OBS, SimHub…)
    with gzip.open(OUT / f"{stem}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(anon, f, ensure_ascii=False)
    with open(src / f"{stem}.csv", encoding="utf-8") as fi, gzip.open(OUT / f"{stem}.csv.gz", "wt", encoding="utf-8") as fo:
        fo.write(fi.read())
    pm = src / f"{stem}.presentmon.csv"
    if pm.is_file():
        with open(pm, newline="", encoding="utf-8-sig", errors="replace") as fi, \
                gzip.open(OUT / f"{stem}.presentmon.csv.gz", "wt", newline="", encoding="utf-8") as fo:
            rd = csv.DictReader(fi)
            w = csv.writer(fo)
            w.writerow(PM_COLS)
            for r in rd:
                row = []
                for c in PM_COLS:
                    v = r.get(c, "")
                    try:
                        v = f"{float(v):.3f}" if c.startswith("Ms") else v
                    except ValueError:
                        pass
                    row.append(v)
                w.writerow(row)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for s in sys.argv[2:]:
        build(Path(sys.argv[1]), s)
        print("ok", s)
