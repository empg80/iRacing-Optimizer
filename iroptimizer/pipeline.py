"""Flujo común para CLI y GUI: analizar una sesión y calcular recomendaciones y perfiles."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import analyzer, config_reader, profiles, recommender, report


@dataclass
class Result:
    analysis: dict
    recs: list[dict]
    profiles: dict[str, list[dict]]
    meta: dict
    html_path: Path
    json_path: Path


def profiles_for(renderer_flat: dict, hardware: dict, analysis: dict | None) -> dict[str, list[dict]]:
    refresh = (hardware.get("display") or {}).get("refresh")
    nvidia = any(g.get("vendor") == "nvidia" for g in hardware.get("gpus") or [])
    return {n: profiles.build_profile(n, renderer_flat, refresh, analysis, nvidia) for n in profiles.ORDER}


def run(csv_path: str | Path, presentmon: str | None = None, target: float | None = None,
        current_renderer_flat: dict | None = None) -> Result:
    """Analiza el CSV. Los perfiles se calculan contra el ini *actual* si se pasa (si no, el del snapshot)."""
    csv_path = Path(csv_path)
    rows, meta = analyzer.load_session(csv_path)
    pm = analyzer.load_presentmon(presentmon) if presentmon else None
    a = analyzer.analyze(rows, meta, pm, target)
    recs = recommender.build(a, meta.get("config") or {})
    flat = current_renderer_flat or config_reader.flat_from_snapshot((meta.get("config") or {}).get("renderer", {}))
    profs = profiles_for(flat, meta.get("hardware") or {}, a)
    html_p, json_p = report.write(a, recs, csv_path, profs)
    return Result(a, recs, profs, meta, html_p, json_p)
