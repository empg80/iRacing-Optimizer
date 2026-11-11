"""Flujo común para CLI y GUI: analizar una sesión y calcular recomendaciones y perfiles."""
from __future__ import annotations

from .i18n import _

from dataclasses import dataclass
from pathlib import Path

from . import analyzer, config_reader, diagnostics, presentmon, profiles, recommender, report


@dataclass
class Result:
    analysis: dict
    recs: list[dict]
    profiles: dict[str, list[dict]]
    diagnostics: dict
    meta: dict
    html_path: Path
    json_path: Path


def profiles_for(renderer_flat: dict, hardware: dict, analysis: dict | None,
                 vr_hz: float | None = None) -> dict[str, list[dict]]:
    refresh = (hardware.get("display") or {}).get("refresh")
    nvidia = any(g.get("vendor") == "nvidia" for g in hardware.get("gpus") or [])
    if vr_hz is None and analysis and analysis.get("vr"):
        vr_hz = analysis["vr"].get("hz")
    return {n: profiles.build_profile(n, renderer_flat, refresh, analysis, nvidia, vr_hz) for n in profiles.ORDER}


def load_pm(csv_path: Path, meta: dict, rows: list[dict], explicit: str | None = None) -> dict | None:
    """Frametimes de PresentMon: el CSV indicado o el que grabó el monitor junto a la sesión.

    Si se esperaba PresentMon y no se puede usar, deja el motivo en meta["presentmon_status"] para que el
    diagnóstico lo muestre (antes se descartaba en silencio).
    """
    info = meta.get("presentmon") or {}
    if info.get("error") and not explicit:
        meta["presentmon_status"] = ("no_start", str(info["error"]))
        return None
    path = Path(explicit) if explicit else (csv_path.parent / Path(info["csv"]).name if info.get("csv") else None)
    if not path:
        return None
    found = presentmon.find_file(path)
    if not found:
        meta["presentmon_status"] = ("missing", path.name)
        return None
    path = found
    try:
        frames = presentmon.load_frames(path, info.get("t0"))
    except (OSError, ValueError) as e:
        meta["presentmon_status"] = ("unreadable", str(e)[:200])
        return None
    flat = config_reader.flat_from_snapshot((meta.get("config") or {}).get("renderer", {}))
    try:
        cap = float(flat.get("desiredfpslimit")) if flat.get("limitframerate") == "1" else None
    except (TypeError, ValueError):
        cap = None
    if (meta.get("vr") or {}).get("active"):
        cap = None
    res = presentmon.analyze(frames, rows, cap)
    if not res:
        meta["presentmon_status"] = ("no_match", str(len(frames)))
        return None
    return res


WATCH_KEYS = ("DesiredFPSLimit", "LimitFrameRate", "MSAASamples", "MaxCarsToDraw", "MaxCarsToDrawInMirrors",
              "ShadowMapType", "MirrorDetail", "ShaderQuality", "ParticleDetail", "LODMinFPSTarget", "NvReflexMode")


def use_final_config(meta: dict) -> None:
    """Si la configuración cambió durante la grabación, el análisis usa la final y guarda la lista de cambios."""
    end = meta.get("config_end")
    if not end or not meta.get("config"):
        return
    a = config_reader.flat_from_snapshot((meta["config"]).get("renderer", {}))
    b = config_reader.flat_from_snapshot(end.get("renderer", {}))
    changes = [(k, a.get(k.lower()), b.get(k.lower())) for k in WATCH_KEYS if a.get(k.lower()) != b.get(k.lower())]
    if changes:
        meta["config_start"], meta["config"] = meta["config"], end
        meta["config_changes"] = changes


def run(csv_path: str | Path, presentmon: str | None = None, target: float | None = None,
        current_renderer_flat: dict | None = None) -> Result:
    """Analiza el CSV. Los perfiles se calculan contra el ini *actual* si se pasa (si no, el del snapshot)."""
    csv_path = Path(csv_path)
    rows, meta = analyzer.load_session(csv_path)
    use_final_config(meta)
    pm = load_pm(csv_path, meta, rows, presentmon)
    a = analyzer.analyze(rows, meta, pm, target)
    recs = recommender.build(a, meta.get("config") or {})
    flat = current_renderer_flat or config_reader.flat_from_snapshot((meta.get("config") or {}).get("renderer", {}))
    profs = profiles_for(flat, meta.get("hardware") or {}, a)
    diag = diagnostics.run(rows, meta, a)
    html_p, json_p = report.write(a, recs, csv_path, profs, diag)
    return Result(a, recs, profs, diag, meta, html_p, json_p)


def compare(csv_a: str | Path, csv_b: str | Path) -> dict:
    """Compara dos sesiones: FPS globales, FPS por tráfico (comparación justa) y ajustes que cambiaron."""
    out = {}
    for tag, p in (("a", csv_a), ("b", csv_b)):
        rows, meta = analyzer.load_session(p)
        a = analyzer.analyze(rows, meta)
        out[tag] = {"name": Path(p).name, "analysis": a, "meta": meta,
                    "flat": config_reader.flat_from_snapshot(((meta.get("config") or {}).get("renderer") or {}).get(
                        "Graphics Options") and {"Graphics Options": meta["config"]["renderer"]["Graphics Options"]}
                        or (meta.get("config") or {}).get("renderer", {}))}
    A, B = out["a"], out["b"]
    ga, gb = A["analysis"]["groups"][A["analysis"]["primary"]], B["analysis"]["groups"][B["analysis"]["primary"]]
    tb = {t["bucket"]: t for t in A["analysis"]["traffic"]}
    traffic = []
    for t in B["analysis"]["traffic"]:
        if t["bucket"] in tb and tb[t["bucket"]]["n"] >= 20 and t["n"] >= 20:
            fa, fb = tb[t["bucket"]]["fps_mean"], t["fps_mean"]
            traffic.append({"bucket": t["bucket"], "a": fa, "b": fb, "delta_pct": 100 * (fb / fa - 1)})
    changed = []
    for k in sorted(set(A["flat"]) | set(B["flat"])):
        va, vb = A["flat"].get(k), B["flat"].get(k)
        if va != vb:
            changed.append({"key": k, "a": va, "b": vb})
    return {
        "a": {"name": A["name"], "session": A["analysis"]["primary"], "fps": ga["fps"], "track": A["meta"].get("track")},
        "b": {"name": B["name"], "session": B["analysis"]["primary"], "fps": gb["fps"], "track": B["meta"].get("track")},
        "traffic": traffic,
        "changed_settings": changed,
        "same_track": A["meta"].get("track") == B["meta"].get("track"),
    }


def compare_text(c: dict) -> str:
    fa, fb = c["a"]["fps"], c["b"]["fps"]
    lines = [f"A: {c['a']['name']}  ({c['a']['session']}, {c['a']['track']})",
             f"B: {c['b']['name']}  ({c['b']['session']}, {c['b']['track']})"]
    if not c["same_track"]:
        lines.append(_("¡Ojo! Circuitos distintos: la comparación es orientativa."))
    lines += ["", f"{'':14}{'A':>8}{'B':>8}"]
    for k, lab in (("mean", _("FPS medio")), ("p5", "FPS p5"), ("p1", "FPS p1")):
        lines.append(f"{lab:14}{fa[k]:8.0f}{fb[k]:8.0f}")
    if c["traffic"]:
        lines += ["", _("FPS por coches cerca (<150 m) — comparación justa:"),
                  f"{_('coches'):14}{'A':>8}{'B':>8}{'Δ':>9}"]
        for t in c["traffic"]:
            lines.append(f"{t['bucket']:14}{t['a']:8.0f}{t['b']:8.0f}{t['delta_pct']:+8.0f}%")
    if c["changed_settings"]:
        lines += ["", _("Ajustes distintos entre A y B:")]
        lines += [f"  {x['key']}: {x['a']} → {x['b']}" for x in c["changed_settings"]]
    return "\n".join(lines)
