"""Monitor de sesión: telemetría de rendimiento del SDK de iRacing + métricas del sistema.

Variables del SDK usadas (1 s de media dentro de iRacing):
  FrameRate, CpuUsageFG (hilo principal/render), CpuUsageBG (física/red), GpuUsage,
  ChanLatency, ChanAvgLatency, ChanQuality, ChanPartnerQuality, ChanClockSkew,
  MemPageFaultSec, MemSoftPageFaultSec, CarIdxLapDistPct, CarIdxTrackSurface.
Además: CPU por núcleo, RAM, RSS del proceso de iRacing y GPU vía NVML (si hay NVIDIA).
"""
from __future__ import annotations

from .i18n import _

import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

import psutil

FIELDS = [
    "t", "session_time", "session_num", "session_type", "on_track", "on_pit_road", "replay",
    "speed", "fps", "cpu_fg", "cpu_bg", "gpu_usage", "chan_latency", "chan_avg_latency",
    "chan_quality", "chan_partner_quality", "clock_skew", "page_faults", "soft_page_faults",
    "cars_in_world", "cars_near", "lap", "lap_pct", "sys_cpu_avg", "sys_cpu_max_core", "sys_ram_used_mb",
    "ir_rss_mb", "gpu_util", "gpu_mem_used_mb", "gpu_temp", "gpu_clock", "gpu_power_w",
    "gpu_throttle", "disk_read_mbs", "disk_write_mbs", "disk_logging",
]

NEAR_METERS = 150.0  # radio para contar coches "cerca" (los que más cuestan de dibujar)


class SystemSampler:
    def __init__(self):
        self.nvml = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self.nvml = pynvml
            self.h = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self.nvml = None
        self.proc: psutil.Process | None = None
        psutil.cpu_percent(percpu=True)  # primera llamada inicializa el contador
        self._proc_cpu: dict[str, float] = {}
        self._proc_samples = 0
        self._last_proc_scan = 0.0
        self._ncpu = psutil.cpu_count() or 1
        self._disk_prev = (time.time(), psutil.disk_io_counters())

    def scan_processes(self, every_s: float = 10.0):
        """Acumula el % de CPU por nombre de proceso (sobre el total de hilos) cada `every_s` segundos."""
        now = time.time()
        if now - self._last_proc_scan < every_s:
            return
        first = self._last_proc_scan == 0.0
        self._last_proc_scan = now
        snap: dict[str, float] = {}
        for p in psutil.process_iter(["name"]):
            try:
                v = p.cpu_percent(None)  # desde la llamada anterior para ese proceso
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            n = p.info["name"] or "?"
            if n.lower() in ("system idle process", "idle"):
                continue
            snap[n] = snap.get(n, 0.0) + v
        if first:
            return  # la primera pasada solo inicializa contadores
        self._proc_samples += 1
        for n, v in snap.items():
            self._proc_cpu[n] = self._proc_cpu.get(n, 0.0) + v / self._ncpu

    def top_processes(self, k: int = 10) -> list[dict]:
        if not self._proc_samples:
            return []
        avg = sorted(((n, v / self._proc_samples) for n, v in self._proc_cpu.items()), key=lambda x: -x[1])
        return [{"name": n, "cpu_pct": round(v, 1), "is_iracing": n.lower().startswith("iracing")}
                for n, v in avg[:k] if v >= 0.3]

    def _find_proc(self):
        if self.proc and self.proc.is_running():
            return self.proc
        self.proc = None
        for p in psutil.process_iter(["name"]):
            n = (p.info["name"] or "").lower()
            if n.startswith("iracingsim"):
                self.proc = p
                break
        return self.proc

    def sample(self) -> dict:
        cores = psutil.cpu_percent(percpu=True)
        vm = psutil.virtual_memory()
        d = {
            "sys_cpu_avg": round(sum(cores) / len(cores), 1) if cores else None,
            "sys_cpu_max_core": max(cores) if cores else None,
            "sys_ram_used_mb": (vm.total - vm.available) // (1024 * 1024),
            "ir_rss_mb": None,
        }
        try:
            now, io = time.time(), psutil.disk_io_counters()
            t0, io0 = self._disk_prev
            dt = max(1e-3, now - t0)
            if io and io0:
                d["disk_read_mbs"] = round((io.read_bytes - io0.read_bytes) / dt / 1e6, 1)
                d["disk_write_mbs"] = round((io.write_bytes - io0.write_bytes) / dt / 1e6, 1)
            self._disk_prev = (now, io)
        except Exception:
            pass
        p = self._find_proc()
        if p:
            try:
                d["ir_rss_mb"] = p.memory_info().rss // (1024 * 1024)
            except Exception:
                pass
        if self.nvml:
            n, h = self.nvml, self.h
            try:
                d["gpu_util"] = n.nvmlDeviceGetUtilizationRates(h).gpu
                d["gpu_mem_used_mb"] = n.nvmlDeviceGetMemoryInfo(h).used // (1024 * 1024)
                d["gpu_temp"] = n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU)
                d["gpu_clock"] = n.nvmlDeviceGetClockInfo(h, n.NVML_CLOCK_GRAPHICS)
                d["gpu_power_w"] = round(n.nvmlDeviceGetPowerUsage(h) / 1000, 1)
                fn = getattr(n, "nvmlDeviceGetCurrentClocksEventReasons", None) or \
                    getattr(n, "nvmlDeviceGetCurrentClocksThrottleReasons", None)
                if fn:
                    d["gpu_throttle"] = int(fn(h))
            except Exception:
                pass
        return d


def _track_length_m(weekend: dict) -> float | None:
    s = str(weekend.get("TrackLength", ""))
    m = re.search(r"([\d.]+)\s*(km|mi)", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1000 if m.group(2) == "km" else v * 1609.344


class SessionMonitor:
    def __init__(self, out_dir: Path, hz: float = 4.0, meta_extra: dict | None = None,
                 presentmon_exe: Path | None = None):
        import irsdk  # pyirsdk
        self.ir = irsdk.IRSDK()
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.hz = hz
        self.sys = SystemSampler()
        self.meta = dict(meta_extra or {})
        self.track_len = None
        self.player_idx = None
        self._sessions: list = []
        self._last_info = 0.0
        self.presentmon_exe = presentmon_exe

    def _connected(self) -> bool:
        ir = self.ir
        if ir.is_initialized and ir.is_connected:
            return True
        if ir.is_initialized:
            ir.shutdown()
        return bool(ir.startup()) and ir.is_initialized and ir.is_connected

    def _refresh_session_info(self, force=False):
        if not force and time.time() - self._last_info < 5:
            return
        self._last_info = time.time()
        ir = self.ir
        try:
            wk = ir["WeekendInfo"] or {}
            self.track_len = _track_length_m(wk) or self.track_len
            self.player_idx = (ir["DriverInfo"] or {}).get("DriverCarIdx", self.player_idx)
            self._sessions = (ir["SessionInfo"] or {}).get("Sessions", []) or self._sessions
            self.meta.setdefault("track", wk.get("TrackDisplayName"))
            self.meta.setdefault("track_config", wk.get("TrackConfigName"))
            self.meta.setdefault("track_length_m", self.track_len)
            drivers = (ir["DriverInfo"] or {}).get("Drivers", [])
            self.meta["max_drivers_seen"] = max(self.meta.get("max_drivers_seen", 0), len(drivers))
            if self.player_idx is not None:
                me = next((d for d in drivers if d.get("CarIdx") == self.player_idx), None)
                if me:
                    self.meta.setdefault("car", me.get("CarScreenName"))
        except Exception:
            pass

    def _session_type(self, num) -> str:
        try:
            return str(self._sessions[num].get("SessionType", "?"))
        except Exception:
            return "?"

    def _cars(self) -> tuple[int, int]:
        ir = self.ir
        pct = ir["CarIdxLapDistPct"] or []
        surf = ir["CarIdxTrackSurface"] or []
        me = self.player_idx
        in_world = sum(1 for s in surf if s is not None and s >= 0)
        if me is None or me >= len(pct) or not self.track_len or pct[me] is None or pct[me] < 0:
            return in_world, 0
        p0, near = pct[me], 0
        for i, p in enumerate(pct):
            if i == me or p is None or p < 0 or i >= len(surf) or surf[i] < 0:
                continue
            d = abs(p - p0)
            d = min(d, 1 - d) * self.track_len
            if d <= NEAR_METERS:
                near += 1
        return in_world, near

    def run(self, duration_s: float | None = None, on_tick=None, stop_event=None,
            log=print, on_row=None) -> tuple[Path | None, Path | None]:
        """Graba hasta Ctrl+C, `stop_event.set()`, fin de `duration_s` o cierre de iRacing."""
        stopped = (lambda: stop_event is not None and stop_event.is_set())
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.out_dir / f"session_{stamp}.csv"
        meta_path = self.out_dir / f"session_{stamp}.json"
        log("Esperando a iRacing…")
        while not self._connected():
            if stopped():
                return None, None
            time.sleep(1)
        log("Conectado. Grabando.")
        self._refresh_session_info(force=True)
        cap = None
        if self.presentmon_exe and (self.meta.get("vr") or {}).get("active"):
            log(_("Sesión VR: PresentMon no se usa (mediría la ventana espejo del monitor, no el visor)."))
            self.presentmon_exe = None
        if self.presentmon_exe:
            from .presentmon import Capture
            cap = Capture(self.presentmon_exe, csv_path.with_suffix(".presentmon.csv"))
            if cap.start():
                self.meta["presentmon"] = {"csv": cap.out_csv.name, "t0": cap.started_at}
                log("PresentMon midiendo frametimes.")
            else:
                self.meta["presentmon"] = {"error": cap.error}
                log(_("PresentMon no pudo arrancar: ") + ("falta permiso (grupo Performance Log Users o administrador)."
                                                       if cap.error == "permiso" else str(cap.error)))
                cap = None
        t0, last_ok, rows = time.time(), time.time(), 0
        period = 1.0 / self.hz
        ir = self.ir
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            try:
                while True:
                    tick = time.time()
                    if stopped() or (duration_s and tick - t0 >= duration_s):
                        break
                    if not self._connected():
                        if tick - last_ok > 10:
                            log(_("iRacing desconectado. Fin de la grabación."))
                            break
                        time.sleep(1)
                        continue
                    last_ok = tick
                    ir.freeze_var_buffer_latest()
                    self._refresh_session_info()
                    snum = ir["SessionNum"]
                    in_world, near = self._cars()
                    row = {
                        "t": round(tick, 3),
                        "session_time": ir["SessionTime"],
                        "session_num": snum,
                        "session_type": self._session_type(snum),
                        "on_track": int(bool(ir["IsOnTrack"])),
                        "on_pit_road": int(bool(ir["OnPitRoad"])),
                        "replay": int(bool(ir["IsReplayPlaying"])),
                        "speed": ir["Speed"],
                        "fps": ir["FrameRate"],
                        "cpu_fg": ir["CpuUsageFG"],
                        "cpu_bg": ir["CpuUsageBG"],
                        "gpu_usage": ir["GpuUsage"],
                        "chan_latency": ir["ChanLatency"],
                        "chan_avg_latency": ir["ChanAvgLatency"],
                        "chan_quality": ir["ChanQuality"],
                        "chan_partner_quality": ir["ChanPartnerQuality"],
                        "clock_skew": ir["ChanClockSkew"],
                        "page_faults": ir["MemPageFaultSec"],
                        "soft_page_faults": ir["MemSoftPageFaultSec"],
                        "cars_in_world": in_world,
                        "cars_near": near,
                        "lap": ir["Lap"],
                        "lap_pct": ir["LapDistPct"],
                        "disk_logging": int(bool(ir["IsDiskLoggingActive"])),
                    }
                    ir.unfreeze_var_buffer_latest()
                    row.update(self.sys.sample())
                    if row["on_track"]:
                        self.sys.scan_processes()
                    w.writerow(row)
                    rows += 1
                    if on_row:
                        on_row(row)
                    if on_tick and rows % max(1, int(self.hz)) == 0:
                        on_tick(row, rows)
                    if rows % int(self.hz * 5) == 0:
                        f.flush()
                        if not on_tick:
                            print(f"\r{row['session_type']:<12} fps {row['fps'] or 0:6.1f} | "
                                  f"cpuFG {row['cpu_fg'] or 0:.2f} | gpu {row.get('gpu_util') or 0} % | "
                                  f"cars {near:2d} | samples {rows}", end="", flush=True)
                    time.sleep(max(0.0, period - (time.time() - tick)))
            except KeyboardInterrupt:
                log(_("Grabación detenida."))
            finally:
                if cap:
                    log("Cerrando PresentMon…")
                    cap.stop()
        self.meta["top_processes"] = [p for p in self.sys.top_processes(12) if not p["is_iracing"]][:8]
        self.meta["iracing_cpu_pct"] = next((p["cpu_pct"] for p in self.sys.top_processes(12) if p["is_iracing"]), None)
        self.meta["samples"] = rows
        self.meta["hz"] = self.hz
        self.meta["started"] = datetime.fromtimestamp(t0).isoformat(timespec="seconds")
        meta_path.write_text(json.dumps(self.meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        try:
            self.ir.shutdown()
        except Exception:
            pass
        return csv_path, meta_path
