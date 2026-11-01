"""Integración con PresentMon (Intel, licencia MIT) para medir frametimes reales.

El SDK de iRacing da FrameRate como media de 1 s: no ve microtirones. PresentMon registra cada frame
(tiempo entre frames, CPU ocupada, GPU ocupada), así que da 1 % / 0,1 % lows reales y dice frame a
frame si limita la CPU o la GPU.

Permisos: PresentMon usa ETW de Windows y necesita que el usuario sea administrador o pertenezca al
grupo "Performance Log Users" (SID S-1-5-32-559; en Windows en español: "Usuarios del registro de
rendimiento"). La app NO se eleva a sí misma: si hace falta, ofrece añadir tu usuario a ese grupo
una sola vez (pide confirmación de Windows/UAC) y después funciona sin administrador.
"""
from __future__ import annotations

from .i18n import _

import csv
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .safety import system_exe

PROCESS_NAMES = ("iRacingSim64DX11.exe", "iRacingSim64DX12.exe")
SESSION = "iRacingOptimizer"
PERF_LOG_USERS_SID = "S-1-5-32-559"

# Alias de columnas entre versiones de PresentMon (1.x, 2.x y actual)
COL_FRAMETIME = ("MsBetweenPresents", "FrameTime", "MsBetweenAppStart", "msBetweenPresents")
COL_CPU = ("MsCPUBusy", "CPUBusy")
COL_GPU = ("MsGPUBusy", "GPUBusy")
COL_TIME_DATE = ("CPUStartDateTime",)
COL_TIME_S = ("TimeInSeconds", "CPUStartTimeInSeconds", "CPUStartTime")
COL_TIME_MS = ("CPUStartTimeInMs",)
COL_APP = ("Application", "ProcessName")


def _resource_dirs() -> list[Path]:
    dirs = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(Path(base) / "tools")
    dirs.append(Path(__file__).resolve().parent.parent / "tools")
    return dirs


def find_exe(extra_dirs: list[Path] | None = None) -> Path | None:
    """PresentMon incluido en el .exe, en ./tools o en Documentos\\iRacingOptimizer\\tools."""
    for d in [*(extra_dirs or []), *_resource_dirs()]:
        if d and d.is_dir():
            cands = sorted(d.glob("PresentMon*.exe"))
            if cands:
                return cands[-1]
    return None


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def in_perf_log_users() -> bool | None:
    """¿El token actual pertenece al grupo? (requiere cerrar sesión tras añadirse)."""
    if sys.platform != "win32":
        return None
    exe = system_exe("whoami.exe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "/groups"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return PERF_LOG_USERS_SID in out
    except Exception:
        return None


def can_capture() -> bool:
    return is_admin() or bool(in_perf_log_users())


def request_group_membership() -> bool:
    """Lanza (con UAC) un PowerShell que añade el usuario actual al grupo. Devuelve si se lanzó."""
    if sys.platform != "win32":
        return False
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    member = f"{domain}\\{user}" if domain else user
    if not re.fullmatch(r"[\w .\-$\\]{1,120}", member, flags=re.UNICODE):
        raise ValueError(_("Nombre de usuario no válido para añadir al grupo."))
    ps = system_exe(r"WindowsPowerShell\v1.0\powershell.exe")
    if not ps:
        return False
    cmd = f"Add-LocalGroupMember -SID '{PERF_LOG_USERS_SID}' -Member '{member}' -ErrorAction Stop"
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", ps, f'-NoProfile -Command "{cmd}"', None, 0)
    return rc > 32


class Capture:
    """Proceso de PresentMon capturando iRacing a un CSV."""

    def __init__(self, exe: Path, out_csv: Path):
        self.exe = Path(exe)
        self.out_csv = Path(out_csv)
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self.started_at: float | None = None

    def start(self, wait_s: float = 2.5) -> bool:
        args = [str(self.exe)]
        for n in PROCESS_NAMES:
            args += ["--process_name", n]
        args += ["--output_file", str(self.out_csv), "--session_name", SESSION, "--stop_existing_session",
                 "--no_console_stats", "--date_time", "--terminate_on_proc_exit"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     creationflags=flags)
        self.started_at = time.time()
        t_end = time.time() + wait_s
        while time.time() < t_end:
            if self.proc.poll() is not None:
                out = (self.proc.stdout.read() if self.proc.stdout else "") or ""
                self.error = "permiso" if "access denied" in out.lower() or "denied" in out.lower() else out.strip()[-300:]
                return False
            time.sleep(0.2)
        return True

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self, timeout_s: float = 8.0) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            # Parar la sesión ETW hace que PresentMon termine limpio y vuelque el CSV
            try:
                subprocess.run([str(self.exe), "--terminate_existing_session", "--session_name", SESSION],
                               capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
            try:
                self.proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()


# ---------------------------------------------------------------------------- lectura y análisis

def _col(fields, names):
    return next((c for c in names if c in fields), None)


def _parse_dt(s: str) -> float | None:
    m = re.match(r"(\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+)\.(\d+)", s.strip())
    if not m:
        return None
    y, mo, d, h, mi, se, frac = m.groups()
    base = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se)).timestamp()
    return base + int(frac) / (10 ** len(frac))


def _fnum(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def load_frames(path: str | Path, t0: float | None = None) -> list[tuple]:
    """Devuelve [(t_abs_o_rel, frametime_ms, cpu_busy_ms|None, gpu_busy_ms|None)] de iRacing."""
    frames = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames or []
        c_ft = _col(fields, COL_FRAMETIME)
        if not c_ft:
            raise ValueError(_("No encuentro la columna de frametime en el CSV de PresentMon"))
        c_cpu, c_gpu = _col(fields, COL_CPU), _col(fields, COL_GPU)
        c_dt, c_s, c_ms = _col(fields, COL_TIME_DATE), _col(fields, COL_TIME_S), _col(fields, COL_TIME_MS)
        c_app = _col(fields, COL_APP)
        for r in rd:
            if c_app and not (r.get(c_app) or "").lower().startswith("iracingsim"):
                continue
            ft = _fnum(r.get(c_ft))
            if not ft or ft <= 0 or ft > 5000:
                continue
            if c_dt:
                t = _parse_dt(r.get(c_dt) or "")
            elif c_s:
                t = _fnum(r.get(c_s))
                t = (t0 + t) if (t is not None and t0) else t
            elif c_ms:
                t = _fnum(r.get(c_ms))
                t = (t0 + t / 1000) if (t is not None and t0) else (t / 1000 if t is not None else None)
            else:
                t = None
            frames.append((t, ft, _fnum(r.get(c_cpu)) if c_cpu else None, _fnum(r.get(c_gpu)) if c_gpu else None))
    return align_clock(frames, t0)


def align_clock(frames: list[tuple], t0: float | None) -> list[tuple]:
    """Corrige un desfase horario entero en las marcas de PresentMon.

    En real (nov-2026, Windows 11, horario de invierno) PresentMon escribió la hora con +1 h respecto al reloj
    del sistema, y ningún frame coincidía con la telemetría. Si el primer frame dista de la hora de arranque de
    la captura (t0) un múltiplo de 15 min (± 2 min), se resta ese desfase.
    """
    if not t0 or not frames or frames[0][0] is None:
        return frames
    d = frames[0][0] - t0
    if abs(d) <= 120:
        return frames
    off = round(d / 900) * 900
    if off and abs(d - off) <= 120 and abs(off) <= 14 * 3600:
        return [(None if t is None else t - off, *rest) for t, *rest in frames]
    return frames


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] if lo == hi else sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def analyze(frames: list[tuple], telemetry: list[dict] | None = None) -> dict:
    """Estadísticas de frametime, tirones reales y quién limita frame a frame.

    Si hay telemetría (filas del monitor con 't' absoluto), solo se usan los frames en pista y cada
    tirón se sitúa en la sesión, minuto y posición de la vuelta.
    """
    tele = sorted((r for r in (telemetry or []) if r.get("t")), key=lambda r: r["t"])
    # Como el resto del informe: si hay carrera (≥ 1 min en pista), las cifras son de la carrera
    race = [r for r in tele if (r.get("session_type") or "").lower() == "race" and r.get("on_track") and not r.get("replay")]
    if len(race) >= 240:
        tele = sorted((r for r in tele if (r.get("session_type") or "").lower() == "race"), key=lambda r: r["t"])
    tt = [r["t"] for r in tele]

    def nearest(t):
        if not tt or t is None:
            return None
        import bisect
        i = bisect.bisect_left(tt, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(tt)), key=lambda j: abs(tt[j] - t))
        return tele[best] if abs(tt[best] - t) < 2.0 else None

    use = []
    for fr in frames:
        if tele and fr[0] is not None:
            r = nearest(fr[0])
            if not r or not r.get("on_track") or r.get("replay"):
                continue
            use.append((fr, r))
        else:
            use.append((fr, None))
    if len(use) < 100:
        return {}
    fts = [u[0][1] for u in use]
    s = sorted(fts)
    mean = sum(fts) / len(fts)
    p99, p999 = _pct(s, 99), _pct(s, 99.9)

    # tirones: frame > 2× la mediana de los 60 anteriores y al menos 8 ms más largo
    from collections import deque
    from statistics import median
    win: deque = deque(maxlen=60)
    stutters = []
    for (t, ft, cpu, gpu), r in use:
        if len(win) >= 30:
            med = median(win)
            if ft > 2 * med and ft - med > 8:
                if cpu is not None and cpu >= 0.8 * ft:
                    cause = "CPU"
                elif gpu is not None and gpu >= 0.8 * ft:
                    cause = "GPU"
                else:
                    cause = _("espera/sincronización")
                stutters.append({"t": t, "ft": ft, "base": med, "cause": cause, "row": r})
        win.append(ft)
    # agrupar tirones cercanos (< 1 s) en incidentes
    events = []
    for st in stutters:
        if events and st["t"] is not None and events[-1]["t_end"] is not None and st["t"] - events[-1]["t_end"] < 1.0:
            ev = events[-1]
            ev["frames"] += 1
            ev["max_ft"] = max(ev["max_ft"], st["ft"])
            ev["t_end"] = st["t"]
            ev["causes"][st["cause"]] = ev["causes"].get(st["cause"], 0) + 1
        else:
            r = st["row"] or {}
            events.append({"t": st["t"], "t_end": st["t"], "frames": 1, "max_ft": st["ft"], "base": st["base"],
                           "causes": {st["cause"]: 1}, "session_time": r.get("session_time"),
                           "session_type": r.get("session_type"), "lap_pct": r.get("lap_pct"),
                           "cars_near": r.get("cars_near"), "on_pit_road": r.get("on_pit_road")})
    for ev in events:
        ev["cause"] = max(ev["causes"], key=ev["causes"].get)

    # quién limita frame a frame (si PresentMon da CPU/GPU busy)
    bound = {"cpu": 0, "gpu": 0}
    n_b = 0
    for (t, ft, cpu, gpu), _row in use:
        if gpu is None or cpu is None:
            continue
        n_b += 1
        if gpu >= 0.9 * ft:
            bound["gpu"] += 1
        else:
            bound["cpu"] += 1
    minutes = max(1e-6, sum(fts) / 60000)
    return {
        "frames": len(fts),
        "minutes": minutes,
        "avg_fps": 1000 / mean,
        "low1_fps": 1000 / p99,
        "low01_fps": 1000 / p999,
        "frametime_p99_ms": p99,
        "frametime_max_ms": s[-1],
        "stutters_over_2x": len(stutters),
        "stutters_per_min": len(stutters) / minutes,
        "stutter_events": sorted(events, key=lambda e: -e["max_ft"])[:40],
        "bound_pct": {k: 100 * v / n_b for k, v in bound.items()} if n_b else None,
        "aligned": bool(tele),
    }
