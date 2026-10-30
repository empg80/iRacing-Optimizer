"""Genera una sesión sintética (práctica + carrera) y unos .ini de ejemplo para probar sin iRacing."""
from __future__ import annotations

import csv
import os
import json
import math
import random
from pathlib import Path

from .monitor import FIELDS

SAMPLE_RENDERER = """[Graphics Options]
ShaderQuality=3                 ; 0=low, 1=med, 2=high, 3=max
ShadowMapType=3                 ; map onto: 0=off, 1=track, 2=track\\cars, 3=track\\cars\\tso
ShadowDetail=1                  ; 0=fewer shadows, 1=maximum shadows
ParticleDetail=2                ; particle detail: 0=low, 1=med, 2=high
DynamicTrackTextureUpdateRate=3 ; 0=min, 1=low, 2=med, 3=high
TwoPassTrees=1                  ; 0=off, 1=render trees with higher quality in two passes
NumFixedCubemaps=0              ; number of fixed cubemaps to render per frame
NumDynamicCubemaps=1            ; number of dynamic cubemaps to render per frame
HeadlightLevel=1                ; 0=low quality, 1=medium, 2=high quality
ParallelSorting=1               ; 0=disabled 1=multithreaded scene sort
MirrorDetail=1                  ; 0=low detail, 1=high detail in mirrors
MaxCockpitMirrors=3             ; Maximum number of cockpit mirrors to enable (0 to 4)
VirtualMirrors=0                ; 0=off, 1=virtual mirrors enabled
SkyDetail=2                     ; 0=low update rate, 1=med update rate, 2=high update rate
DynamicLODSpacing=4             ; 0=off, 10=max
MaxAniso=16                     ; 1=off, or 2, 4, 8, 16
LODMinFPSTarget=60              ; Reduce LODs when FPS is below target
MaxPitObjsToDrawInMirrors=8     ; 0 to 192
MaxPitObjsToDraw=30             ; 0 to 192
MaxCarsToDrawInMirrors=16       ; 4 to 64
MaxCarsToDraw=40                ; 10 to 64
OcclusionCull=1                 ; 0=disable occlusion culling, 1=enabled
LimitFrameRate=0                ; 0=no limit, 1=use DesiredFPSLimit
DesiredFPSLimit=240             ; Enabled when LimitFrameRate=1

[Memory]
VidMemMB=8192                   ; video memory to use
MaxWorkingSetMB_64Bit=12000     ; system memory to use

[MonitorSetup]
NumMonitors=1                   ; 1 or 3
"""

SAMPLE_APP = """[Graphics]
maxCars=40                      ; max cars sent by server
"""


def _row(t, st, stype, base_fps, near, rng):
    # Modelo de juguete: el hilo FG sufre con coches cerca; la GPU va alta en pista despejada
    cpu_fg = min(1.0, 0.55 + 0.045 * near + rng.uniform(-0.04, 0.04))
    fps = base_fps * (1 - 0.035 * near) * rng.uniform(0.96, 1.03)
    gpu = min(1.0, 0.97 - 0.03 * near + rng.uniform(-0.03, 0.02))
    return {
        "t": t, "session_time": st, "session_num": 1 if stype == "Race" else 0, "session_type": stype,
        "on_track": 1, "on_pit_road": 0, "replay": 0, "speed": 55,
        "fps": round(fps, 1), "cpu_fg": round(cpu_fg, 3), "cpu_bg": round(rng.uniform(0.2, 0.35), 3),
        "gpu_usage": round(gpu, 3), "chan_latency": rng.uniform(0.03, 0.06), "chan_avg_latency": 0.045,
        "chan_quality": rng.uniform(0.97, 1.0), "chan_partner_quality": rng.uniform(0.95, 1.0),
        "clock_skew": 0.0, "page_faults": rng.uniform(0, 40), "soft_page_faults": rng.uniform(500, 2000),
        "cars_in_world": 40, "cars_near": near, "sys_cpu_avg": 18, "sys_cpu_max_core": 70 + near * 3,
        "sys_ram_used_mb": 21000, "ir_rss_mb": 9800, "gpu_util": round(gpu * 100), "gpu_mem_used_mb": 13500,
        "gpu_temp": 71, "gpu_clock": 2700, "gpu_power_w": 380,
    }


def make(out_dir: Path, with_presentmon: bool = False) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    ini_dir = out_dir / "demo_iracing_ini"
    ini_dir.mkdir(parents=True, exist_ok=True)
    (ini_dir / "rendererDX11Monitor.ini").write_text(SAMPLE_RENDERER, encoding="utf-8")
    (ini_dir / "app.ini").write_text(SAMPLE_APP, encoding="utf-8")

    rng = random.Random(7)
    rows, t = [], 1_800_000_000.0
    for i in range(5 * 60 * 4):  # 5 min práctica, poco tráfico
        near = 1 if rng.random() < 0.15 else 0
        rows.append(_row(t, i / 4, "Practice", 205, near, rng)); t += 0.25
    t += 120
    for i in range(20 * 60 * 4):  # 20 min carrera: salida con pelotón y luego se estira
        m = i / 240
        mean_near = 11 * math.exp(-m / 2.5) + 1.2 + (2 if 9 < m < 11 else 0)
        near = max(0, int(rng.gauss(mean_near, 1.3)))
        rows.append(_row(t, i / 4, "Race", 200, near, rng)); t += 0.25

    csv_path = out_dir / "session_demo.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    from . import config_reader
    meta = {
        "track": "Nürburgring Combined (demo)", "car": "Porsche 911 GT3 R (992)", "max_drivers_seen": 40,
        "started": "2026-10-30T20:00:00", "samples": len(rows), "hz": 4,
        "hardware": {"os": "Windows 11", "cpu": {"name": "AMD Ryzen 7 7800X3D 8-Core Processor", "cores_physical": 8,
                     "cores_logical": 16, "x3d": True}, "ram_mb": 65536,
                     "gpus": [{"name": "NVIDIA GeForce RTX 4090", "vram_mb": 24564, "driver": "demo", "vendor": "nvidia"}],
                     "display": {"width": 5120, "height": 1440, "refresh": 240},
                     "windows": {"power_plan": {"name": "Equilibrado"}, "hags": True, "game_mode": True}},
        "config": config_reader.snapshot(config_reader.load(str(ini_dir))),
    }
    # PresentMon sintético para la carrera: frames con tirones puntuales al llegar tráfico
    pm_path = csv_path.with_suffix(".presentmon.csv")
    from datetime import datetime as _dt
    with (open(pm_path, "w", newline="", encoding="utf-8") if with_presentmon else open(os.devnull, "w")) as f:
        w = csv.writer(f)
        w.writerow(["Application", "ProcessID", "CPUStartDateTime", "MsBetweenPresents", "MsCPUBusy", "MsGPUBusy"])
        for r in rows:
            if not with_presentmon or r["session_type"] != "Race":
                continue
            fps = r["fps"]
            t = r["t"]
            n = max(1, int(fps * 0.25))
            for k in range(n):
                ft = 1000 / fps * rng.uniform(0.97, 1.03)
                if rng.random() < 0.0004 + 0.0004 * r["cars_near"]:
                    ft *= rng.uniform(2.5, 4)
                cpu = ft * (0.55 + 0.04 * r["cars_near"]) if ft < 30 else ft * 0.9
                gpu = ft * r["gpu_usage"] * 0.95
                tt = t + k / n * 0.25
                stamp = _dt.fromtimestamp(tt).strftime("%Y-%m-%d %H:%M:%S") + f".{int((tt % 1) * 1e9):09d}"
                w.writerow(["iRacingSim64DX11.exe", 1234, stamp, f"{ft:.4f}", f"{min(cpu, ft):.4f}", f"{min(gpu, ft):.4f}"])
    if with_presentmon:
        meta["presentmon"] = {"csv": pm_path.name}
    meta_path = csv_path.with_suffix(".json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return csv_path, ini_dir
