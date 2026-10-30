"""Interfaz gráfica (tkinter, sin dependencias extra) para iRacing Optimizer."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, apply as applier, config_reader, hardware, pipeline, profiles

APP = "iRacing Optimizer"
PRIO_TAG = {1: "p1", 2: "p2", 3: "p3"}
BN = {"cap": "En el tope", "gpu": "GPU", "cpu": "CPU / motor", "mixed": "Mixto", "other": "Sin datos GPU"}
CHECK, UNCHECK = "☑", "☐"


def resource(name: str) -> Path:
    """Ruta a un recurso, tanto en desarrollo como dentro del .exe de PyInstaller."""
    base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
    return Path(base) / "assets" / name


def default_out_dir() -> Path:
    d = config_reader.documents_dir() / "iRacingOptimizer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_path(p: Path):
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(p)])


class CheckTree(ttk.Treeview):
    """Treeview con una primera columna de casillas que se marcan al hacer clic."""

    def __init__(self, master, columns, widths, **kw):
        super().__init__(master, columns=["sel", *columns], show="headings", **kw)
        self.heading("sel", text="")
        self.column("sel", width=34, anchor="center", stretch=False)
        for c, w in zip(columns, widths):
            self.heading(c, text=c)
            self.column(c, width=w, anchor="w", stretch=c == columns[-1])
        self.bind("<Button-1>", self._click)
        self.payload: dict[str, dict] = {}

    def _click(self, e):
        if self.identify_region(e.x, e.y) != "cell" or self.identify_column(e.x) != "#1":
            return
        iid = self.identify_row(e.y)
        if iid and self.set(iid, "sel") in (CHECK, UNCHECK):
            self.set(iid, "sel", UNCHECK if self.set(iid, "sel") == CHECK else CHECK)

    def clear(self):
        self.delete(*self.get_children())
        self.payload.clear()

    def add(self, values, data=None, checked=False, checkable=True, tags=()):
        iid = self.insert("", "end", values=[(CHECK if checked else UNCHECK) if checkable else "", *values], tags=tags)
        if data is not None:
            self.payload[iid] = data
        return iid

    def checked(self) -> list[dict]:
        return [self.payload[i] for i in self.get_children() if self.set(i, "sel") == CHECK and i in self.payload]

    def set_all(self, pred):
        for i in self.get_children():
            if i in self.payload:
                self.set(i, "sel", CHECK if pred(self.payload[i]) else UNCHECK)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {__version__}")
        self.geometry("1180x760")
        self.minsize(980, 620)
        try:
            self._icon = tk.PhotoImage(file=str(resource("icon.png")))
            self.iconphoto(True, self._icon)
            if sys.platform == "win32":
                self.iconbitmap(default=str(resource("icon.ico")))
        except Exception:
            pass
        self.q: queue.Queue = queue.Queue()
        self.cfg: config_reader.IRacingConfig | None = None
        self.hw: dict = {}
        self.result: pipeline.Result | None = None
        self.out_dir = default_out_dir()
        self.ini_dir_override: str | None = None
        self.mon_thread: threading.Thread | None = None
        self.mon_stop = threading.Event()

        self._style()
        self._build()
        self.after(100, self._poll)
        self._bg(self._load_all, "Cargando equipo y configuración…")

    # ------------------------------------------------------------------ infra
    def _style(self):
        s = ttk.Style(self)
        if "vista" in s.theme_names():
            s.theme_use("vista")
        elif "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Big.TLabel", font=("Segoe UI", 20, "bold"))
        s.configure("Muted.TLabel", foreground="#666")
        s.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _bg(self, fn, msg=None):
        if msg:
            self.status(msg)

        def run():
            try:
                fn()
            except Exception as e:  # mostrar cualquier error en la UI
                self.q.put(("error", f"{e}\n\n{traceback.format_exc(limit=3)}"))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "error":
                    self.status("Error")
                    messagebox.showerror(APP, payload)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def ui(self, fn):
        """Ejecuta fn en el hilo de la interfaz."""
        self.q.put(("call", fn))

    def status(self, text):
        self.status_var.set(text)

    # ------------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Carpeta de iRacing:").pack(side="left")
        self.dir_var = tk.StringVar(value="—")
        ttk.Label(top, textvariable=self.dir_var, style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Button(top, text="Cambiar…", command=self.choose_dir).pack(side="left")
        ttk.Label(top, text="   Archivo gráfico:").pack(side="left")
        self.renderer_var = tk.StringVar()
        self.renderer_cb = ttk.Combobox(top, textvariable=self.renderer_var, state="readonly", width=28)
        self.renderer_cb.pack(side="left", padx=6)
        self.renderer_cb.bind("<<ComboboxSelected>>", lambda e: self._bg(self._load_cfg, "Cargando…"))
        ttk.Button(top, text="Recargar", command=lambda: self._bg(self._load_cfg, "Recargando…")).pack(side="left")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._tab_equipo()
        self._tab_monitor()
        self._tab_analisis()
        self._tab_perfiles()
        self._tab_copias()

        self.status_var = tk.StringVar(value="Listo")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 3), relief="sunken").pack(fill="x")

    def _tab_equipo(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Equipo y configuración")
        self.hw_text = tk.Text(f, height=9, wrap="word", relief="flat", font=("Segoe UI", 10))
        self.hw_text.pack(fill="x")
        ttk.Label(f, text="Ajustes actuales del archivo gráfico").pack(anchor="w", pady=(10, 4))
        self.cfg_tree = ttk.Treeview(f, columns=("sec", "key", "val"), show="headings")
        for c, t, w in (("sec", "Sección", 180), ("key", "Clave", 280), ("val", "Valor", 200)):
            self.cfg_tree.heading(c, text=t)
            self.cfg_tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.cfg_tree.yview)
        self.cfg_tree.configure(yscrollcommand=sb.set)
        self.cfg_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _tab_monitor(self):
        f = ttk.Frame(self.nb, padding=14)
        self.nb.add(f, text="Monitor de sesión")
        ttk.Label(f, text="Arranca la grabación y entra a pista (mejor en carrera). Detén al terminar.",
                  style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=10)
        self.btn_start = ttk.Button(row, text="▶  Iniciar grabación", style="Accent.TButton", command=self.start_monitor)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="■  Detener", command=self.stop_monitor, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.auto_analyze = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Analizar al terminar", variable=self.auto_analyze).pack(side="left", padx=12)
        ttk.Label(row, text="Muestras/s:").pack(side="left")
        self.hz_var = tk.DoubleVar(value=4.0)
        ttk.Spinbox(row, from_=1, to=10, increment=1, width=4, textvariable=self.hz_var).pack(side="left", padx=4)

        grid = ttk.Frame(f)
        grid.pack(anchor="w", pady=10)
        self.live = {}
        for i, (k, t) in enumerate((("state", "Estado"), ("session", "Sesión"), ("fps", "FPS"), ("gpu", "GPU %"),
                                    ("cpu", "CpuUsageFG"), ("near", "Coches cerca"), ("samples", "Muestras"),
                                    ("elapsed", "Tiempo"))):
            box = ttk.Frame(grid, padding=(0, 0, 28, 10))
            box.grid(row=i // 4, column=i % 4, sticky="w")
            ttk.Label(box, text=t, style="Muted.TLabel").pack(anchor="w")
            v = tk.StringVar(value="—")
            ttk.Label(box, textvariable=v, style="Big.TLabel").pack(anchor="w")
            self.live[k] = v
        self.mon_log = tk.Text(f, height=8, relief="flat", font=("Consolas", 9))
        self.mon_log.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(f, text=f"Las sesiones se guardan en {self.out_dir}", style="Muted.TLabel").pack(anchor="w", pady=4)

    def _tab_analisis(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Análisis y recomendaciones")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Abrir sesión (.csv)…", command=self.open_session).pack(side="left")
        ttk.Label(row, text="   FPS objetivo (0 = auto):").pack(side="left")
        self.target_var = tk.IntVar(value=0)
        ttk.Spinbox(row, from_=0, to=500, increment=5, width=6, textvariable=self.target_var).pack(side="left", padx=4)
        ttk.Button(row, text="Reanalizar", command=self.reanalyze).pack(side="left", padx=4)
        ttk.Button(row, text="Abrir informe HTML", command=self.open_report).pack(side="right")
        ttk.Button(row, text="Comparar con otra sesión…", command=self.compare_sessions).pack(side="right", padx=6)
        self.session_var = tk.StringVar(value="Ninguna sesión cargada")
        ttk.Label(f, textvariable=self.session_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 2))

        kp = ttk.Frame(f)
        kp.pack(fill="x", pady=6)
        self.kpi = {}
        for k, t in (("mean", "FPS medio"), ("p5", "FPS p5"), ("p1", "FPS p1"), ("target", "Objetivo"),
                     ("below", "Bajo objetivo"), ("limit", "Te limita")):
            box = ttk.Frame(kp, padding=(0, 0, 26, 0))
            box.pack(side="left")
            ttk.Label(box, text=t, style="Muted.TLabel").pack(anchor="w")
            v = tk.StringVar(value="—")
            ttk.Label(box, textvariable=v, style="Big.TLabel").pack(anchor="w")
            self.kpi[k] = v

        self.rec_tree = CheckTree(f, ["Prio", "Área", "Clave", "Actual", "Propuesto", "Motivo"],
                                  [50, 110, 210, 80, 90, 520], height=12)
        self.rec_tree.tag_configure("p1", foreground="#c0392b")
        self.rec_tree.tag_configure("p2", foreground="#b9770e")
        self.rec_tree.tag_configure("p3", foreground="#555")
        self.rec_tree.tag_configure("info", foreground="#1f618d")
        self.rec_tree.pack(fill="both", expand=True, pady=6)
        self.rec_tree.bind("<Double-1>", self._show_rec)
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Marcar P1", command=lambda: self.rec_tree.set_all(lambda r: r["priority"] == 1)).pack(side="left")
        ttk.Button(btns, text="Marcar P1 + P2", command=lambda: self.rec_tree.set_all(lambda r: r["priority"] <= 2)).pack(side="left", padx=4)
        ttk.Button(btns, text="Desmarcar todo", command=lambda: self.rec_tree.set_all(lambda r: False)).pack(side="left")
        ttk.Label(btns, text="  Doble clic: ver motivo completo", style="Muted.TLabel").pack(side="left")
        ttk.Button(btns, text="Aplicar seleccionados (con copia de seguridad)", style="Accent.TButton",
                   command=self.apply_selected).pack(side="right")

    def _tab_perfiles(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Perfiles")
        self.prof_hint = tk.StringVar(value="Perfiles genéricos. Si cargas una sesión, el tope de FPS y el LOD dinámico se ajustan a tu medición.")
        ttk.Label(f, textvariable=self.prof_hint, style="Muted.TLabel", wraplength=1100).pack(anchor="w", pady=(0, 8))
        cols = ttk.Frame(f)
        cols.pack(fill="both", expand=True)
        self.prof_trees = {}
        for i, name in enumerate(profiles.ORDER):
            p = profiles.PROFILES[name]
            lf = ttk.LabelFrame(cols, text=p["title"], padding=8)
            lf.grid(row=0, column=i, sticky="nsew", padx=4)
            cols.columnconfigure(i, weight=1)
            cols.rowconfigure(0, weight=1)
            ttk.Label(lf, text=p["desc"], wraplength=330).pack(anchor="w")
            tv = ttk.Treeview(lf, columns=("key", "cur", "new"), show="headings", height=16)
            for c, t, w in (("key", "Clave", 170), ("cur", "Actual", 60), ("new", "Nuevo", 60)):
                tv.heading(c, text=t)
                tv.column(c, width=w, anchor="w")
            tv.pack(fill="both", expand=True, pady=6)
            ttk.Button(lf, text=f"Aplicar «{p['title']}»", style="Accent.TButton",
                       command=lambda n=name: self.apply_profile(n)).pack(fill="x")
            self.prof_trees[name] = tv

    def _tab_copias(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Copias de seguridad")
        ttk.Label(f, text="Cada vez que aplicas cambios se guarda una copia de todos los .ini de iRacing.",
                  style="Muted.TLabel").pack(anchor="w")
        self.bk_list = tk.Listbox(f, height=14, font=("Consolas", 10))
        self.bk_list.pack(fill="both", expand=True, pady=8)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Restaurar seleccionada", style="Accent.TButton", command=self.restore_selected).pack(side="left")
        ttk.Button(row, text="Crear copia ahora", command=self.backup_now).pack(side="left", padx=6)
        ttk.Button(row, text="Abrir carpeta", command=self.open_backups).pack(side="left")

    # ------------------------------------------------------------------ carga
    def _load_all(self):
        self.hw = hardware.get_hardware()
        self._load_cfg()

    def _load_cfg(self):
        renderer = self.renderer_var.get() or None
        try:
            cfg = config_reader.load(self.ini_dir_override, renderer)
        except FileNotFoundError as e:
            msg = str(e)
            self.ui(lambda: (self.status(msg), messagebox.showwarning(APP, f"{msg}\n\nUsa «Cambiar…» para indicar la carpeta.")))
            self.ui(self._refresh_hw)
            return
        self.cfg = cfg
        self.ui(self._refresh_all)

    def _refresh_all(self):
        self._refresh_hw()
        c = self.cfg
        self.dir_var.set(str(c.directory))
        self.renderer_cb["values"] = [n for n, _ in c.renderer_candidates]
        if c.renderer_name:
            self.renderer_var.set(c.renderer_name)
        self.cfg_tree.delete(*self.cfg_tree.get_children())
        if c.renderer:
            for sec, kv in c.renderer.as_dict().items():
                for k, v in kv.items():
                    self.cfg_tree.insert("", "end", values=(sec, k, v))
        self._refresh_profiles()
        self._refresh_backups()
        self.status(f"Configuración cargada: {c.renderer_name or 'sin archivo gráfico'}")

    def _refresh_hw(self):
        h = self.hw
        if not h:
            return
        g = (h.get("gpus") or [{}])[0]
        d = h.get("display") or {}
        w = h.get("windows") or {}
        lines = [
            f"CPU: {h['cpu']['name']}  ({h['cpu']['cores_physical']} núcleos / {h['cpu']['cores_logical']} hilos)",
            f"RAM: {h['ram_mb'] / 1024:.0f} GB",
            f"GPU: {g.get('name', 'no detectada')}  {g.get('vram_mb', 0) / 1024:.0f} GB VRAM  · driver {g.get('driver', '?')}",
            f"Pantalla principal: {d.get('width', '?')}×{d.get('height', '?')} @ {d.get('refresh', '?')} Hz",
            f"Plan de energía: {(w.get('power_plan') or {}).get('name', '?')}   ·   Sistema: {h.get('os', '')}",
        ]
        self.hw_text.configure(state="normal")
        self.hw_text.delete("1.0", "end")
        self.hw_text.insert("1.0", "\n".join(lines))
        self.hw_text.configure(state="disabled")

    def _refresh_profiles(self):
        if not self.cfg or not self.cfg.renderer:
            return
        analysis = self.result.analysis if self.result else None
        profs = pipeline.profiles_for(self.cfg.renderer.flat(), self.hw, analysis)
        self._profiles = profs
        for name, tv in self.prof_trees.items():
            tv.delete(*tv.get_children())
            ch = profs[name]
            if not ch:
                tv.insert("", "end", values=("(ya coincide)", "", ""))
            for c in ch:
                tv.insert("", "end", values=(c["key"], c["current"], c["proposed"]))
        if analysis:
            f = analysis["groups"][analysis["primary"]]["fps"]
            self.prof_hint.set(f"Ajustados a tu sesión: mantienes {f['p10']:.0f} fps el 90 % del tiempo (mediana {f['median']:.0f}). "
                               "Tras aplicar un perfil, graba otra sesión para afinar el tope.")

    def _refresh_backups(self):
        self.bk_list.delete(0, "end")
        if not self.cfg:
            return
        for d in applier.list_backups(self.cfg.directory):
            files = ", ".join(sorted(p.name for p in d.iterdir()))
            self.bk_list.insert("end", f"{d.name:<38} {files}")

    def choose_dir(self):
        d = filedialog.askdirectory(title="Carpeta Documentos\\iRacing")
        if d:
            self.ini_dir_override = d
            self.renderer_var.set("")
            self._bg(self._load_cfg, "Cargando…")

    # ------------------------------------------------------------------ monitor
    def _log(self, text):
        self.ui(lambda: (self.mon_log.insert("end", time.strftime("%H:%M:%S ") + text + "\n"), self.mon_log.see("end")))

    def start_monitor(self):
        if not self.cfg:
            messagebox.showwarning(APP, "Primero carga la carpeta de iRacing.")
            return
        try:
            import irsdk  # noqa: F401  (solo comprobar que está)
            del irsdk
        except ImportError:
            messagebox.showerror(APP, "Falta pyirsdk (pip install pyirsdk).")
            return
        self.mon_stop.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.live["state"].set("Esperando")
        t0 = [None]

        def tick(row, n):
            if t0[0] is None:
                t0[0] = time.time()
            el = int(time.time() - t0[0])

            def upd():
                self.live["state"].set("Grabando")
                self.live["session"].set(row.get("session_type") or "—")
                self.live["fps"].set(f"{row.get('fps') or 0:.0f}")
                g = row.get("gpu_util")
                self.live["gpu"].set("—" if g is None else f"{g:.0f}")
                self.live["cpu"].set(f"{(row.get('cpu_fg') or 0):.2f}")
                self.live["near"].set(str(row.get("cars_near", "—")))
                self.live["samples"].set(str(n))
                self.live["elapsed"].set(f"{el // 60}:{el % 60:02d}")
            self.ui(upd)

        def run():
            from .monitor import SessionMonitor
            meta = {"hardware": self.hw, "config": config_reader.snapshot(self.cfg)}
            mon = SessionMonitor(self.out_dir, hz=float(self.hz_var.get() or 4), meta_extra=meta)
            csv_path, _ = mon.run(on_tick=tick, stop_event=self.mon_stop, log=self._log)

            def done():
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.live["state"].set("Parado")
                if csv_path:
                    self._log(f"Guardado: {csv_path}")
                    if self.auto_analyze.get():
                        self.analyze(csv_path)
            self.ui(done)

        self.mon_thread = threading.Thread(target=lambda: self._safe(run), daemon=True)
        self.mon_thread.start()
        self.status("Monitor activo")

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            self.q.put(("error", f"{e}\n\n{traceback.format_exc(limit=3)}"))
            self.ui(lambda: (self.btn_start.configure(state="normal"), self.btn_stop.configure(state="disabled")))

    def stop_monitor(self):
        self.mon_stop.set()
        self.status("Deteniendo…")

    # ------------------------------------------------------------------ análisis
    def open_session(self):
        p = filedialog.askopenfilename(title="Sesión grabada", initialdir=self.out_dir,
                                       filetypes=[("Sesiones", "session_*.csv"), ("CSV", "*.csv")])
        if p:
            self.analyze(Path(p))

    def reanalyze(self):
        if self.result:
            self.analyze(self._csv)

    def analyze(self, csv_path: Path):
        self._csv = Path(csv_path)
        target = self.target_var.get() or None
        flat = self.cfg.renderer.flat() if self.cfg and self.cfg.renderer else None

        def work():
            res = pipeline.run(self._csv, None, target, flat)
            self.result = res
            self.ui(self._show_result)
        self._bg(work, f"Analizando {self._csv.name}…")

    def _show_result(self):
        r = self.result
        a = r.analysis
        g = a["groups"][a["primary"]]
        f = g["fps"]
        meta = a["meta"]
        self.session_var.set(f"{self._csv.name} · {meta.get('track') or ''} · {meta.get('car') or ''} · "
                             f"{a['primary']} · {g['duration_min']:.0f} min")
        self.kpi["mean"].set(f"{f['mean']:.0f}")
        self.kpi["p5"].set(f"{f['p5']:.0f}")
        self.kpi["p1"].set(f"{f['p1']:.0f}")
        self.kpi["target"].set(f"{a['target_fps']:.0f}")
        self.kpi["below"].set(f"{g['below_target_pct']:.0f} %")
        top = max(g["bottleneck"], key=g["bottleneck"].get)
        self.kpi["limit"].set(f"{BN[top]} {g['bottleneck'][top]:.0f} %")
        t = self.rec_tree
        t.clear()
        for rec in r.recs:
            if rec.get("key"):
                t.add([f"P{rec['priority']}", rec["category"], rec["key"], rec["current"], rec["proposed"], rec["reason"]],
                      data=rec, checked=rec["priority"] == 1, tags=(PRIO_TAG[rec["priority"]],))
            else:
                t.add([f"P{rec['priority']}", rec["category"], rec["title"], "", "", rec["reason"]],
                      checkable=False, tags=("info",))
        self._refresh_profiles()
        self.nb.select(2)
        self.status(f"Análisis listo. Informe: {r.html_path}")

    def _show_rec(self, e):
        iid = self.rec_tree.identify_row(e.y)
        if iid:
            v = self.rec_tree.item(iid, "values")
            messagebox.showinfo(v[3], v[6])

    def compare_sessions(self):
        if not self.result:
            messagebox.showinfo(APP, "Primero abre o graba una sesión (será la sesión B, la nueva).")
            return
        p = filedialog.askopenfilename(title="Sesión ANTERIOR (A) para comparar", initialdir=self.out_dir,
                                       filetypes=[("Sesiones", "session_*.csv"), ("CSV", "*.csv")])
        if not p:
            return
        b = self._csv

        def work():
            txt = pipeline.compare_text(pipeline.compare(p, b))

            def show():
                w = tk.Toplevel(self)
                w.title("Comparación de sesiones")
                t = tk.Text(w, width=78, height=34, font=("Consolas", 10))
                t.insert("1.0", txt)
                t.configure(state="disabled")
                t.pack(fill="both", expand=True, padx=8, pady=8)
            self.ui(show)
        self._bg(work, "Comparando…")

    def open_report(self):
        if self.result:
            webbrowser.open(self.result.html_path.resolve().as_uri())

    # ------------------------------------------------------------------ aplicar
    def _confirm_apply(self, changes, what) -> bool:
        if not changes:
            messagebox.showinfo(APP, "No hay cambios que aplicar.")
            return False
        if applier.iracing_running():
            messagebox.showwarning(APP, "Cierra iRacing antes de aplicar cambios: el simulador reescribe los .ini al salir.")
            return False
        listado = "\n".join(f"• {c['key']}: {c['current']} → {c['proposed']}" for c in changes[:25])
        extra = f"\n… y {len(changes) - 25} más" if len(changes) > 25 else ""
        return messagebox.askyesno(APP, f"Aplicar {what} ({len(changes)} cambios)?\n\n{listado}{extra}\n\n"
                                        "Se hará antes una copia de seguridad de todos los .ini.")

    def _do_apply(self, changes, label):
        try:
            log = applier.apply_changes(changes, self.cfg.directory, self.cfg.renderer_name, label=label)
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return
        messagebox.showinfo(APP, "\n".join(log[:30]))
        self._bg(self._load_cfg, "Recargando configuración…")

    def apply_selected(self):
        if not self.cfg:
            return
        ch = self.rec_tree.checked()
        if self._confirm_apply(ch, "las recomendaciones marcadas"):
            self._do_apply(ch, "recomendaciones")

    def apply_profile(self, name):
        if not self.cfg:
            return
        ch = getattr(self, "_profiles", {}).get(name, [])
        if self._confirm_apply(ch, f"el perfil «{profiles.PROFILES[name]['title']}»"):
            self._do_apply(ch, f"perfil_{name}")

    # ------------------------------------------------------------------ copias
    def backup_now(self):
        if self.cfg:
            d = applier.backup(self.cfg.directory, "manual")
            self._refresh_backups()
            self.status(f"Copia creada: {d.name}")

    def restore_selected(self):
        if not self.cfg:
            return
        sel = self.bk_list.curselection()
        if not sel:
            messagebox.showinfo(APP, "Selecciona una copia.")
            return
        stamp = self.bk_list.get(sel[0]).split()[0]
        if not messagebox.askyesno(APP, f"Restaurar la copia {stamp}?\nSe sobrescribirán los .ini actuales "
                                        "(antes se guarda una copia del estado actual)."):
            return
        try:
            applier.backup(self.cfg.directory, "antes_de_restaurar")
            log = applier.restore(self.cfg.directory, stamp)
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return
        messagebox.showinfo(APP, "\n".join(log))
        self._bg(self._load_cfg, "Recargando…")

    def open_backups(self):
        if self.cfg:
            d = Path(self.cfg.directory) / applier.BACKUP_DIR
            d.mkdir(exist_ok=True)
            open_path(d)


def main():
    if sys.platform == "win32":
        try:  # nitidez en pantallas con escalado
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("empg80.iRacingOptimizer")
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
