"""Análisis de una sesión grabada: FPS, cuellos de botella, tráfico, memoria y red."""
from __future__ import annotations

from .i18n import _

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from .config_reader import flat_from_snapshot

# Umbrales heurísticos de clasificación (calibrados con una sesión real 7800X3D + 4090)
GPU_BOUND = 90.0      # % uso GPU (NVML) -> limita la GPU
GPU_IDLE = 80.0       # % por debajo: la GPU espera -> limita CPU/motor (hilo principal, draw calls)
CPU_FG_BOUND = 85.0   # solo si no hay datos de GPU
CAP_MARGIN = 0.96     # fps >= cap*0.96 -> limitado por el tope
WARMUP_S = 5.0        # descarta segundos tras entrar a pista


def _f(x):
    if x is None or x == "":
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def pct(values, p):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(values) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {}
    mean = sum(v) / len(v)
    sd = math.sqrt(sum((x - mean) ** 2 for x in v) / len(v))
    return {"n": len(v), "mean": mean, "median": pct(v, 50), "p1": pct(v, 1), "p5": pct(v, 5),
            "p10": pct(v, 10), "p25": pct(v, 25), "p95": pct(v, 95), "min": min(v), "max": max(v), "sd": sd}


def load_session(csv_path: str | Path) -> tuple[list[dict], dict]:
    csv_path = Path(csv_path)
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    meta_path = csv_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    for r in rows:
        for k, v in r.items():
            if k != "session_type":
                r[k] = _f(v)
    return rows, meta


def _norm_pct(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if vals and pct(vals, 95) <= 1.5:  # el SDK da fracciones (0..1, a veces >1 en picos)
        for r in rows:
            if r.get(key) is not None:
                r[key] = r[key] * 100


def _target(cfg: dict, hw: dict) -> tuple[float, float | None, bool]:
    limit = _f(cfg.get("limitframerate"))
    desired = _f(cfg.get("desiredfpslimit"))
    refresh = _f((hw.get("display") or {}).get("refresh"))
    capped = bool(limit) and bool(desired)
    target = desired if capped else (refresh or 60.0)
    return target, (desired if capped else None), capped


def _gpu(r):
    """NVML es el dato fiable de carga de GPU; GpuUsage del SDK mide otra cosa (sale muy bajo)."""
    return r.get("gpu_util") if r.get("gpu_util") is not None else r.get("gpu_usage")


def _classify(r, cap) -> str:
    fps = r.get("fps") or 0
    gpu = _gpu(r)
    if cap and fps >= cap * CAP_MARGIN:
        return "cap"
    if gpu is not None:
        if gpu >= GPU_BOUND:
            return "gpu"
        if gpu < GPU_IDLE:
            return "cpu"  # la GPU está esperando frames: limita el hilo principal / draw calls
        return "mixed"
    cpu = r.get("cpu_fg")
    return "cpu" if cpu is not None and cpu >= CPU_FG_BOUND else "other"


def _bucket(n):
    if n is None:
        return None
    n = int(n)
    return "0" if n == 0 else "1-2" if n <= 2 else "3-5" if n <= 5 else "6-9" if n <= 9 else "10+"


def _corr(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 10:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    sx = math.sqrt(sum((x - mx) ** 2 for x, _y in pts))
    sy = math.sqrt(sum((y - my) ** 2 for _x, y in pts))
    return sxy / (sx * sy) if sx and sy else None


def _valid(rows):
    out, entered = [], None
    for r in rows:
        if not r.get("on_track") or r.get("replay") or not r.get("fps"):
            entered = None
            continue
        if entered is None:
            entered = r["t"]
        if r["t"] - entered < WARMUP_S:
            continue
        out.append(r)
    return out


def _drops(rows, median, cap):
    thr = 0.8 * median
    events, cur = [], []
    for r in rows:
        if r["fps"] < thr:
            if cur and r["t"] - cur[-1]["t"] > 2.0:
                events.append(cur)
                cur = []
            cur.append(r)
        elif cur:
            events.append(cur)
            cur = []
    if cur:
        events.append(cur)
    out = []
    for ev in events:
        dur = ev[-1]["t"] - ev[0]["t"]
        if dur < 1.0 and len(ev) < 2:
            continue
        kinds = Counter(_classify(r, cap) for r in ev)
        out.append({
            "session_time": ev[0].get("session_time"),
            "session_type": ev[0].get("session_type"),
            "duration_s": round(dur, 1),
            "min_fps": min(r["fps"] for r in ev),
            "cars_near_max": int(max((r.get("cars_near") or 0) for r in ev)),
            "bottleneck": kinds.most_common(1)[0][0],
        })
    out.sort(key=lambda e: (e["min_fps"], -e["duration_s"]))
    return out


def analyze(rows: list[dict], meta: dict, presentmon: dict | None = None, target_override: float | None = None) -> dict:
    hw = meta.get("hardware", {})
    cfg = flat_from_snapshot((meta.get("config") or {}).get("renderer", {}))
    for k in ("cpu_fg", "cpu_bg", "gpu_usage", "chan_quality", "chan_partner_quality"):
        _norm_pct(rows, k)
    target, cap, capped = _target(cfg, hw)
    from . import vr as _vr
    vr_mode = _vr.is_vr_session(meta)
    if vr_mode:  # en VR el objetivo es la frecuencia del visor; el tope de FPS no aplica
        target, cap, capped = float((meta.get("vr") or {}).get("hz") or 90), None, False
    if target_override:
        target = float(target_override)
    valid = _valid(rows)
    if not valid:
        raise ValueError(_("No hay muestras válidas en pista (¿grabaste con el coche en pista?)."))
    vr_selected = vr_detected = None
    if vr_mode:
        vr_selected = target
        if not target_override:
            vr_detected = _vr.detect_rate([r.get("fps") for r in valid], target, (meta.get("vr") or {}).get("headset"))
            if vr_detected:
                target = vr_detected  # el visor estaba a otra frecuencia: se analiza con la real
        cap = target  # al ritmo del visor el juego va sincronizado: cuenta como «en el tope», no como límite de CPU

    groups = defaultdict(list)
    for r in valid:
        groups[r.get("session_type") or "?"].append(r)
    race_key = next((k for k in groups if k.lower() == "race" and len(groups[k]) >= 60), None)
    primary_key = race_key or max(groups, key=lambda k: len(groups[k]))
    prim = groups[primary_key]

    def stats_for(rs):
        fps = [r["fps"] for r in rs]
        kinds = Counter(_classify(r, cap) for r in rs)
        n = len(rs)
        return {
            "fps": summarize(fps),
            "below_target_pct": 100 * sum(1 for x in fps if x < target * 0.95) / n,
            "cpu_fg": summarize([r.get("cpu_fg") for r in rs]),
            "cpu_bg": summarize([r.get("cpu_bg") for r in rs]),
            "gpu": summarize([_gpu(r) for r in rs]),
            "gpu_power": summarize([r.get("gpu_power_w") for r in rs]),
            "sys_cpu_avg": summarize([r.get("sys_cpu_avg") for r in rs]),
            "fps_pit": summarize([r["fps"] for r in rs if r.get("on_pit_road")]),
            "fps_track": summarize([r["fps"] for r in rs if not r.get("on_pit_road")]),
            "bottleneck": {k: 100 * kinds.get(k, 0) / n for k in ("cap", "gpu", "cpu", "mixed", "other")},
            "duration_min": (rs[-1]["t"] - rs[0]["t"]) / 60 if n > 1 else 0,
        }

    group_stats = {k: stats_for(v) for k, v in groups.items()}
    p = group_stats[primary_key]

    # Rendimiento según tráfico cercano
    tb = defaultdict(list)
    for r in prim:
        b = _bucket(r.get("cars_near"))
        if b:
            tb[b].append(r["fps"])
    order = ["0", "1-2", "3-5", "6-9", "10+"]
    traffic = [{"bucket": b, "n": len(tb[b]), "fps_mean": sum(tb[b]) / len(tb[b]),
                "fps_p5": pct(tb[b], 5)} for b in order if tb.get(b)]

    # Rendimiento sin tráfico vs. con tráfico (aísla el coste de dibujar coches)
    alone = [x["fps_mean"] for x in traffic if x["bucket"] == "0"]
    busy = [x for x in traffic if x["bucket"] in ("6-9", "10+") and x["n"] >= 8]
    traffic_drop = None
    if alone and busy:
        traffic_drop = 100 * (1 - min(b["fps_mean"] for b in busy) / alone[0])

    # Zonas lentas del circuito (requiere lap_pct, grabado desde v0.2)
    zones = []
    zb = defaultdict(list)
    for r in prim:
        lp = r.get("lap_pct")
        if lp is not None and 0 <= lp <= 1 and not r.get("on_pit_road"):
            zb[min(39, int(lp * 40))].append(r)
    if len(zb) >= 20:
        for z, rs in zb.items():
            if len(rs) >= 8:
                zones.append({"from_pct": z * 2.5, "to_pct": z * 2.5 + 2.5, "n": len(rs),
                              "fps_mean": sum(x["fps"] for x in rs) / len(rs),
                              "cars_near_mean": sum((x.get("cars_near") or 0) for x in rs) / len(rs),
                              "track_m": (z * 2.5 / 100) * (meta.get("track_length_m") or 0)})
        zones.sort(key=lambda z: z["fps_mean"])

    gpus = hw.get("gpus") or [{}]
    memory = {
        "vram_total_mb": gpus[0].get("vram_mb"),
        "vram_used_max_mb": summarize([r.get("gpu_mem_used_mb") for r in valid]).get("max"),
        "vidmem_cfg_mb": _f(cfg.get("vidmemtousemb") or cfg.get("vidmemmb")),
        "vidmem_key": "VidMemToUseMB" if cfg.get("vidmemtousemb") is not None else "VidMemMB",
        "ram_total_mb": hw.get("ram_mb"),
        "ram_used_max_mb": summarize([r.get("sys_ram_used_mb") for r in valid]).get("max"),
        "ir_rss_max_mb": summarize([r.get("ir_rss_mb") for r in valid]).get("max"),
        "maxworkingset_cfg_mb": _f(cfg.get("sysmemtousemb") or cfg.get("maxworkingsetmb_64bit")),
        "sysmem_key": "SysMemToUseMB" if cfg.get("sysmemtousemb") is not None else "MaxWorkingSetMB_64Bit",
        "page_faults_p95": summarize([r.get("page_faults") for r in valid]).get("p95"),
    }
    network = {
        "quality_mean": summarize([r.get("chan_quality") for r in valid]).get("mean"),
        "quality_p5": summarize([r.get("chan_quality") for r in valid]).get("p5"),
        "partner_quality_mean": summarize([r.get("chan_partner_quality") for r in valid]).get("mean"),
        "latency_p95_ms": (lambda v: v * 1000 if v is not None else None)(
            summarize([r.get("chan_latency") for r in valid]).get("p95")),
    }
    thermal = {
        "gpu_temp_max": summarize([r.get("gpu_temp") for r in valid]).get("max"),
        "gpu_power_max_w": summarize([r.get("gpu_power_w") for r in valid]).get("max"),
    }

    step = max(1, len(prim) // 700)
    t0 = prim[0]["t"]
    series = [{"t": round((r["t"] - t0) / 60, 3), "fps": r["fps"],
               "gpu": _gpu(r),
               "cpu_fg": r.get("cpu_fg"), "cars_near": r.get("cars_near")} for r in prim[::step]]

    return {
        "meta": {k: meta.get(k) for k in ("track", "track_config", "car", "max_drivers_seen", "started", "samples", "hz")},
        "hardware": hw,
        "renderer_file": (meta.get("config") or {}).get("renderer_file"),
        "target_fps": target,
        "fps_cap": cap,
        "capped": capped,
        "primary": primary_key,
        "groups": group_stats,
        "traffic": traffic,
        "slow_zones": zones[:6],
        "traffic_drop_pct": traffic_drop,
        "traffic_corr": _corr([r.get("cars_near") for r in prim], [r["fps"] for r in prim]),
        "drops": _drops(prim, p["fps"]["median"], cap)[:12],
        "memory": memory,
        "top_processes": meta.get("top_processes") or [],
        "vr": ({**_vr.analyze(prim, target), "hz_selected": vr_selected, "hz_detected": vr_detected,
                "renderer_ok": (meta.get("config") or {}).get("renderer_file") in _vr.VR_RENDERERS,
                "runtime": (meta.get("vr") or {}).get("runtime"),
                "software": (meta.get("vr") or {}).get("software") or [],
                "headset": (meta.get("vr") or {}).get("headset")} if vr_mode else None),
        "network": network,
        "thermal": thermal,
        "presentmon": presentmon or None,
        "series": series,
    }
