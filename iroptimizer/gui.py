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

import math

from .safety import RENDERER_FILES
from . import __version__, apply as applier, config_reader, diagnostics, fov, hardware, pipeline, profiles, userprofiles

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
        subprocess.Popen(["/usr/bin/xdg-open", str(p)])  # noqa: S603 (solo desarrollo en Linux)


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
        self._tab_diagnostico()
        self._tab_analisis()
        self._tab_perfiles()
        self._tab_misperfiles()
        self._tab_fov()
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
                                    ("bg", "Hilo física/red %"), ("lat", "Latencia ms"), ("near", "Coches cerca"),
                                    ("elapsed", "Tiempo"))):
            box = ttk.Frame(grid, padding=(0, 0, 28, 10))
            box.grid(row=i // 4, column=i % 4, sticky="w")
            ttk.Label(box, text=t, style="Muted.TLabel").pack(anchor="w")
            v = tk.StringVar(value="—")
            ttk.Label(box, textvariable=v, style="Big.TLabel").pack(anchor="w")
            self.live[k] = v
        ttk.Label(f, text="Registro y alertas en vivo (tirones, latencia, pérdida de paquetes, hilo de física, temperatura de GPU…)",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        self.mon_log = tk.Text(f, height=8, relief="flat", font=("Consolas", 9))
        self.mon_log.tag_configure("alert", foreground="#c0392b")
        self.mon_log.pack(fill="both", expand=True, pady=(4, 0))
        ttk.Label(f, text=f"Las sesiones se guardan en {self.out_dir}", style="Muted.TLabel").pack(anchor="w", pady=4)

    def _tab_diagnostico(self):
        f = ttk.Frame(self.nb, padding=10)
        self.tab_diag = f
        self.nb.add(f, text="Diagnóstico")
        self.diag_head = tk.StringVar(value="Graba o abre una sesión para ver el diagnóstico de CPU, GPU, frametime, red, memoria y sistema.")
        ttk.Label(f, textvariable=self.diag_head, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        pw = ttk.PanedWindow(f, orient="vertical")
        pw.pack(fill="both", expand=True, pady=6)
        top = ttk.Frame(pw)
        self.diag_tree = ttk.Treeview(top, columns=("st", "area", "title", "metric"), show="headings", height=10)
        for c, t, w in (("st", "Estado", 90), ("area", "Área", 90), ("title", "Comprobación", 270), ("metric", "Medición", 640)):
            self.diag_tree.heading(c, text=t)
            self.diag_tree.column(c, width=w, anchor="w", stretch=c == "metric")
        for tag, col in (("problema", "#c0392b"), ("aviso", "#b9770e"), ("ok", "#2e7d4f"), ("info", "#1f618d")):
            self.diag_tree.tag_configure(tag, foreground=col)
        self.diag_tree.pack(fill="both", expand=True)
        self.diag_tree.bind("<<TreeviewSelect>>", self._diag_select)
        pw.add(top, weight=3)
        mid = ttk.LabelFrame(pw, text="Qué significa y cómo solucionarlo", padding=6)
        self.diag_text = tk.Text(mid, height=7, wrap="word", relief="flat", font=("Segoe UI", 10))
        self.diag_text.pack(fill="both", expand=True)
        pw.add(mid, weight=2)
        bot = ttk.LabelFrame(pw, text="Incidentes detectados", padding=6)
        self.ev_tree = ttk.Treeview(bot, columns=("ses", "min", "lap", "kind", "dur", "det"), show="headings", height=6)
        for c, t, w in (("ses", "Sesión", 90), ("min", "Minuto", 70), ("lap", "Vuelta", 70), ("kind", "Tipo", 150),
                        ("dur", "Duración", 80), ("det", "Detalle", 600)):
            self.ev_tree.heading(c, text=t)
            self.ev_tree.column(c, width=w, anchor="w", stretch=c == "det")
        self.ev_tree.pack(fill="both", expand=True)
        pw.add(bot, weight=2)

    def _show_diag(self):
        d = self.result.diagnostics
        sm = d.get("summary", {})
        self.diag_head.set(f"{sm.get('problema', 0)} problemas · {sm.get('aviso', 0)} avisos · {sm.get('info', 0)} info · "
                           f"{sm.get('ok', 0)} correctos")
        self.diag_tree.delete(*self.diag_tree.get_children())
        self._diag_items = {}
        icon = {"problema": "✖ Problema", "aviso": "▲ Aviso", "ok": "✔ OK", "info": "ℹ Info"}
        for c in d["checks"]:
            iid = self.diag_tree.insert("", "end", values=(icon[c["status"]], c["area"], c["title"], c["metric"]),
                                        tags=(c["status"],))
            self._diag_items[iid] = c
        self.ev_tree.delete(*self.ev_tree.get_children())
        for e in d.get("events", []):
            lap = "" if e.get("lap_pct") is None else f"{e['lap_pct'] * 100:.0f} %"
            self.ev_tree.insert("", "end", values=(e.get("session_type") or "", f"{(e.get('session_time') or 0) / 60:.1f}′",
                                                   lap, e["kind"], f"{e['duration_s']} s", e["detail"]))
        kids = self.diag_tree.get_children()
        if kids:
            self.diag_tree.selection_set(kids[0])

    def _diag_select(self, _e=None):
        sel = self.diag_tree.selection()
        if not sel or sel[0] not in getattr(self, "_diag_items", {}):
            return
        c = self._diag_items[sel[0]]
        txt = c["detail"]
        if c["actions"]:
            txt += "\n\nQué hacer:\n" + "\n".join(f"  • {x}" for x in c["actions"])
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", "end")
        self.diag_text.insert("1.0", txt)
        self.diag_text.configure(state="disabled")

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
        self._refresh_myprofiles()
        self._fov_load_from_ini(silent=True)
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
    def _log(self, text, alert=False):
        tag = ("alert",) if alert else ()
        self.ui(lambda: (self.mon_log.insert("end", time.strftime("%H:%M:%S ") + ("⚠ " if alert else "") + text + "\n", tag),
                         self.mon_log.see("end")))

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
        watch = diagnostics.LiveWatch(hz=float(self.hz_var.get() or 4))

        def on_row(row):
            for msg in watch.feed(row):
                self._log(msg, alert=True)

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
                bg = row.get("cpu_bg")
                self.live["bg"].set("—" if bg is None else f"{bg * 100 if bg <= 1.5 else bg:.0f}")
                lat = row.get("chan_latency")
                self.live["lat"].set("—" if lat is None else f"{lat * 1000:.0f}")
                self.live["near"].set(str(row.get("cars_near", "—")))
                self.live["elapsed"].set(f"{el // 60}:{el % 60:02d}")
            self.ui(upd)

        def run():
            from .monitor import SessionMonitor
            meta = {"hardware": self.hw, "config": config_reader.snapshot(self.cfg)}
            mon = SessionMonitor(self.out_dir, hz=float(self.hz_var.get() or 4), meta_extra=meta)
            csv_path, _ = mon.run(on_tick=tick, stop_event=self.mon_stop, log=self._log, on_row=on_row)

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
        self._show_diag()
        self.nb.select(self.tab_diag)
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


    # ------------------------------------------------------------------ mis perfiles
    def _tab_misperfiles(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Mis perfiles")
        ttk.Label(f, text="Guarda tu configuración actual con un nombre (p. ej. «Monitor carreras», «VR», «Grabar vídeos») "
                          "y aplícala cuando quieras. Útil si alternas monitor y gafas VR o distintos usos.",
                  style="Muted.TLabel", wraplength=1100).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Guardar configuración actual…", style="Accent.TButton", command=self.myprof_save).pack(side="left")
        ttk.Button(row, text="Importar…", command=self.myprof_import).pack(side="left", padx=6)
        ttk.Button(row, text="Exportar…", command=self.myprof_export).pack(side="left")
        ttk.Button(row, text="Eliminar", command=self.myprof_delete).pack(side="left", padx=6)
        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        self.mp_tree = ttk.Treeview(left, columns=("name", "file", "inc", "date"), show="headings", height=14)
        for c, t, w in (("name", "Perfil", 200), ("file", "Archivo gráfico", 190), ("inc", "Incluye", 200), ("date", "Guardado", 130)):
            self.mp_tree.heading(c, text=t)
            self.mp_tree.column(c, width=w, anchor="w")
        self.mp_tree.pack(fill="both", expand=True)
        self.mp_tree.bind("<<TreeviewSelect>>", lambda e: self._myprof_diff())
        body.add(left, weight=3)
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        tr = ttk.Frame(right)
        tr.pack(fill="x")
        ttk.Label(tr, text="Aplicar sobre:").pack(side="left")
        self.mp_target = tk.StringVar()
        self.mp_target_cb = ttk.Combobox(tr, textvariable=self.mp_target, state="readonly", width=28)
        self.mp_target_cb.pack(side="left", padx=6)
        self.mp_target_cb.bind("<<ComboboxSelected>>", lambda e: self._myprof_diff())
        self.mp_desc = tk.StringVar(value="Selecciona un perfil para ver qué cambiaría.")
        ttk.Label(right, textvariable=self.mp_desc, style="Muted.TLabel", wraplength=480).pack(anchor="w", pady=6)
        self.mp_diff = ttk.Treeview(right, columns=("file", "key", "cur", "new"), show="headings", height=12)
        for c, t, w in (("file", "Archivo", 150), ("key", "Clave", 170), ("cur", "Actual", 80), ("new", "Perfil", 80)):
            self.mp_diff.heading(c, text=t)
            self.mp_diff.column(c, width=w, anchor="w")
        self.mp_diff.pack(fill="both", expand=True)
        ttk.Button(right, text="Aplicar perfil seleccionado (con copia de seguridad)", style="Accent.TButton",
                   command=self.myprof_apply).pack(fill="x", pady=(6, 0))
        body.add(right, weight=2)
        self._mp_items: dict[str, dict] = {}

    def _refresh_myprofiles(self):
        self.mp_tree.delete(*self.mp_tree.get_children())
        self._mp_items = {}
        for p in userprofiles.list_profiles(self.out_dir):
            inc = ", ".join(userprofiles.GROUPS[g]["label"].split(" (")[0] for g in p.get("groups", []) if g in userprofiles.GROUPS)
            iid = self.mp_tree.insert("", "end", values=(p["name"], p.get("renderer_file", ""), inc, p.get("created", "")[:16].replace("T", " ")))
            self._mp_items[iid] = p
        if self.cfg:
            self.mp_target_cb["values"] = [n for n, _ in self.cfg.renderer_candidates]

    def _mp_selected(self):
        sel = self.mp_tree.selection()
        return self._mp_items.get(sel[0]) if sel else None

    def _myprof_diff(self):
        p = self._mp_selected()
        self.mp_diff.delete(*self.mp_diff.get_children())
        if not p or not self.cfg:
            return
        if self.mp_target.get() not in (self.mp_target_cb["values"] or ()):
            self.mp_target.set(p.get("renderer_file") or (self.cfg.renderer_name or ""))
        if self.mp_target.get() not in RENDERER_FILES:
            self.mp_target.set("rendererDX11Monitor.ini")
        target = self.mp_target.get()
        if not (Path(self.cfg.directory) / target).is_file():
            self.mp_desc.set(f"{target} no existe en tu carpeta de iRacing. Arranca iRacing una vez con ese modo "
                             "(monitor o VR) para que lo cree, o elige otro archivo.")
            self._mp_changes = []
            return
        ch = userprofiles.changes_for(p, self.cfg.directory, target)
        self._mp_changes = ch
        for c in ch:
            self.mp_diff.insert("", "end", values=(c["file_name"], c["key"], c["current"], c["proposed"]))
        desc = p.get("description") or ""
        self.mp_desc.set((desc + " · " if desc else "") + (f"{len(ch)} cambios respecto a tu configuración actual."
                                                          if ch else "Tu configuración ya coincide con este perfil."))

    def myprof_save(self):
        if not self.cfg:
            return
        w = tk.Toplevel(self)
        w.title("Guardar perfil")
        w.transient(self)
        w.grab_set()
        frm = ttk.Frame(w, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Nombre:").grid(row=0, column=0, sticky="w")
        name = tk.StringVar(value="Monitor carreras" if "Monitor" in (self.cfg.renderer_name or "") else "VR")
        ttk.Entry(frm, textvariable=name, width=36).grid(row=0, column=1, sticky="we", pady=3)
        ttk.Label(frm, text="Descripción:").grid(row=1, column=0, sticky="w")
        desc = tk.StringVar()
        ttk.Entry(frm, textvariable=desc, width=36).grid(row=1, column=1, sticky="we", pady=3)
        ttk.Label(frm, text="Archivo gráfico:").grid(row=2, column=0, sticky="w")
        rf = tk.StringVar(value=self.cfg.renderer_name or "")
        ttk.Combobox(frm, textvariable=rf, state="readonly", width=34,
                     values=[n for n, _ in self.cfg.renderer_candidates]).grid(row=2, column=1, sticky="we", pady=3)
        ttk.Label(frm, text="Incluir:").grid(row=3, column=0, sticky="nw", pady=(8, 0))
        gv = {}
        for i, (g, spec) in enumerate(userprofiles.GROUPS.items()):
            gv[g] = tk.BooleanVar(value=g != "pantalla")
            ttk.Checkbutton(frm, text=spec["label"], variable=gv[g]).grid(row=3 + i, column=1, sticky="w", pady=(8 if i == 0 else 0, 0))

        def ok():
            groups = [g for g, v in gv.items() if v.get()]
            if not name.get().strip() or not groups:
                messagebox.showwarning(APP, "Pon un nombre y elige al menos un grupo.", parent=w)
                return
            existing = {p["name"].lower() for p in userprofiles.list_profiles(self.out_dir)}
            if name.get().strip().lower() in existing and not messagebox.askyesno(APP, "Ya existe un perfil con ese nombre. ¿Sobrescribir?", parent=w):
                return
            userprofiles.save(self.out_dir, name.get().strip(), self.cfg.directory, rf.get(), groups, desc.get().strip())
            w.destroy()
            self._refresh_myprofiles()
            self.status(f"Perfil «{name.get().strip()}» guardado")
        ttk.Button(frm, text="Guardar", style="Accent.TButton", command=ok).grid(row=7, column=1, sticky="e", pady=(12, 0))

    def myprof_apply(self):
        p = self._mp_selected()
        if not p or not self.cfg:
            messagebox.showinfo(APP, "Selecciona un perfil.")
            return
        self._myprof_diff()
        ch = getattr(self, "_mp_changes", [])
        if self._confirm_apply(ch, f"el perfil «{p['name']}» sobre {self.mp_target.get()}"):
            try:
                log = applier.apply_changes(ch, self.cfg.directory, self.mp_target.get(), label=f"mi_perfil_{p['name']}")
            except Exception as e:
                messagebox.showerror(APP, str(e))
                return
            extra = ""
            if "OpenXR" in self.mp_target.get() or "OpenVR" in self.mp_target.get() or "Oculus" in self.mp_target.get():
                extra = "\n\nRecuerda elegir el modo VR en la pantalla de iRacing antes de entrar al simulador."
            messagebox.showinfo(APP, "\n".join(log[:30]) + extra)
            self._bg(self._load_cfg, "Recargando configuración…")

    def myprof_delete(self):
        p = self._mp_selected()
        if p and messagebox.askyesno(APP, f"¿Eliminar el perfil «{p['name']}»?"):
            userprofiles.delete(p["_path"])
            self._refresh_myprofiles()

    def myprof_export(self):
        p = self._mp_selected()
        if not p:
            messagebox.showinfo(APP, "Selecciona un perfil.")
            return
        dst = filedialog.asksaveasfilename(defaultextension=".json", initialfile=Path(p["_path"]).name,
                                           filetypes=[("Perfil", "*.json")])
        if dst:
            Path(dst).write_text(Path(p["_path"]).read_text(encoding="utf-8"), encoding="utf-8")
            self.status(f"Exportado a {dst}")

    def myprof_import(self):
        src = filedialog.askopenfilename(filetypes=[("Perfil", "*.json")])
        if src:
            try:
                dst = userprofiles.import_file(self.out_dir, src)
                d = userprofiles.validate(__import__("json").loads(Path(src).read_text(encoding="utf-8")))
                if d.get("_dropped"):
                    messagebox.showwarning(APP, f"Perfil importado en {dst.name}. Se descartaron {d['_dropped']} valores "
                                                "que no parecían ajustes válidos de iRacing.")
            except Exception as e:
                messagebox.showerror(APP, str(e))
                return
            self._refresh_myprofiles()

    # ------------------------------------------------------------------ FOV
    def _tab_fov(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="FOV")
        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        right = ttk.Frame(f)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        self.fv = {k: tk.DoubleVar(value=v) for k, v in (("width", 620), ("bezel", 10), ("dist", 650), ("angle", 50),
                                                         ("radius", 1000), ("diag", 27))}
        self.fv_type = tk.StringVar(value="1 plano")
        self.fv_3p = tk.BooleanVar(value=True)
        self.fv_aspect = tk.StringVar(value="16:9")

        lf = ttk.LabelFrame(left, text="Tu configuración", padding=8)
        lf.pack(fill="x")
        ttk.Label(lf, text="Monitores:").grid(row=0, column=0, sticky="w")
        tf = ttk.Frame(lf)
        tf.grid(row=0, column=1, sticky="w")
        for t in ("1 plano", "1 curvo", "3 planos", "3 curvos"):
            ttk.Radiobutton(tf, text=t, value=t, variable=self.fv_type, command=self._fov_update).pack(side="left")
        r = 1

        def field(label, key, hint="", frm=lf):
            nonlocal r
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
            e = ttk.Spinbox(frm, from_=0, to=5000, increment=1, width=8, textvariable=self.fv[key], command=self._fov_update)
            e.grid(row=r, column=1, sticky="w")
            e.bind("<KeyRelease>", lambda ev: self._fov_update())
            if hint:
                ttk.Label(frm, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=4)
            r += 1
            return e
        field("Ancho del monitor (mm)", "width", "con biseles, recto")
        field("Bisel (mm, un lado)", "bezel")
        self.fv_radius_w = field("Radio de curvatura (mm)", "radius", "1000R = 1000")
        self.fv_angle_w = field("Ángulo de los laterales (°)", "angle")
        ttk.Label(lf, text="Distancia ojos → pantalla (mm)").grid(row=r, column=0, sticky="w", pady=(8, 2))
        sc = ttk.Scale(lf, from_=300, to=1300, length=230, variable=self.fv["dist"], command=lambda v: self._fov_update())
        sc.grid(row=r, column=1, columnspan=2, sticky="we", pady=(8, 2))
        r += 1
        self.fv_dist_lbl = tk.StringVar()
        ttk.Label(lf, textvariable=self.fv_dist_lbl, style="Muted.TLabel").grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(lf, text="Render scene using 3 projections", variable=self.fv_3p,
                        command=self._fov_update).grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))

        hf = ttk.LabelFrame(left, text="¿No sabes el ancho? Calcúlalo desde las pulgadas", padding=8)
        hf.pack(fill="x", pady=8)
        ttk.Label(hf, text="Diagonal (\")").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(hf, from_=10, to=100, increment=1, width=6, textvariable=self.fv["diag"]).grid(row=0, column=1, sticky="w")
        ttk.Label(hf, text="Formato").grid(row=0, column=2, padx=(10, 0))
        ttk.Combobox(hf, textvariable=self.fv_aspect, values=list(fov.ASPECTS), width=6, state="readonly").grid(row=0, column=3)
        ttk.Button(hf, text="Calcular ancho", command=self._fov_from_diag).grid(row=0, column=4, padx=8)
        ttk.Label(hf, text="Da el ancho visible; súmale los dos biseles.", style="Muted.TLabel").grid(row=1, column=0, columnspan=5, sticky="w")

        res = ttk.LabelFrame(left, text="Resultado", padding=8)
        res.pack(fill="x")
        self.fv_out = tk.StringVar(value="—")
        ttk.Label(res, textvariable=self.fv_out, style="Big.TLabel").pack(anchor="w")
        self.fv_detail = tk.StringVar()
        ttk.Label(res, textvariable=self.fv_detail, wraplength=380, justify="left").pack(anchor="w", pady=(4, 0))
        self.fv_warn = tk.StringVar()
        ttk.Label(res, textvariable=self.fv_warn, foreground="#b9770e", wraplength=380, justify="left").pack(anchor="w", pady=(4, 0))
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="Leer de iRacing", command=self._fov_load_from_ini).pack(side="left")
        ttk.Button(btns, text="Usar ángulo ideal", command=self._fov_ideal).pack(side="left", padx=6)
        ttk.Button(btns, text="Aplicar a iRacing", style="Accent.TButton", command=self._fov_apply).pack(side="right")

        self.fv_canvas = tk.Canvas(right, background="#11151d", highlightthickness=0)
        self.fv_canvas.pack(fill="both", expand=True)
        self.fv_canvas.bind("<Configure>", lambda e: self._fov_update())
        ttk.Label(right, text="Vista cenital: tus ojos abajo, pantallas a escala. En verde el FOV calculado; en gris "
                              "discontinuo el FOV que tienes ahora en iRacing.", style="Muted.TLabel", wraplength=460).pack(anchor="w", pady=4)
        self._fov_current = None
        self._fov_result = None

    def _fov_setup(self) -> fov.Setup:
        t = self.fv_type.get()
        g = lambda k: float(self.fv[k].get() or 0)
        return fov.Setup(monitors=3 if t.startswith("3") else 1, curved="curvo" in t, monitor_width=g("width"),
                         bezel=g("bezel"), distance=max(100.0, g("dist")), side_angle=g("angle"),
                         radius=max(100.0, g("radius")), three_projections=self.fv_3p.get())

    def _fov_update(self):
        try:
            s = self._fov_setup()
            res = fov.compute(s)
        except (tk.TclError, ValueError):
            return
        self._fov_result = res
        t = self.fv_type.get()
        state_r = "normal" if "curvo" in t else "disabled"
        state_a = "normal" if t == "3 planos" else "disabled"
        self.fv_radius_w.configure(state=state_r)
        self.fv_angle_w.configure(state=state_a)
        self.fv_dist_lbl.set(f"{s.distance:.0f} mm")
        self.fv_out.set(f"FOV {res.fov_h:.0f}°")
        det = [f"Pantalla visible: {s.screen_width:.0f} mm por monitor."]
        if s.monitors == 3:
            det.append(f"Monitor central: {res.fov_center:.0f}°.")
        if res.ideal_side_angle is not None:
            det.append(f"Ángulo ideal de los laterales: {res.ideal_side_angle:.0f}° (cada lateral mirando a tus ojos).")
        if self._fov_current:
            diff = self._fov_current - res.fov_h
            det.append(f"Ahora en iRacing: {self._fov_current:.0f}° ({'+' if diff >= 0 else ''}{diff:.0f}°"
                       + (", ves más de lo real: los coches parecen más lejos y lentos)." if diff > 3 else
                          ", ves menos de lo real: todo parece más grande y rápido)." if diff < -3 else ", prácticamente correcto)."))
        det.append("Tras aplicar, puedes confirmarlo en iRacing: Ajustes → Pantalla → Monitor → Compute.")
        self.fv_detail.set(" ".join(det))
        self.fv_warn.set("\n".join("⚠ " + w for w in res.warnings))
        self._fov_draw(s, res)

    def _fov_draw(self, s, res):
        c = self.fv_canvas
        c.delete("all")
        W, H = max(200, c.winfo_width()), max(200, c.winfo_height())
        pts = [p for poly in res.outline for p in poly] + [(0.0, 0.0)]
        xs = [p[0] for p in pts]
        zs = [p[1] for p in pts]
        span_x = max(abs(min(xs)), abs(max(xs))) * 2 or 1
        span_z = (max(zs) - min(min(zs), 0)) or 1
        scale = min((W - 80) / span_x, (H - 120) / span_z)
        zmin = min(min(zs), 0)
        ox = W / 2
        oy = H / 2 + (span_z * scale) / 2 + zmin * scale  # centra el dibujo en vertical

        def P(x, z):
            return ox + x * scale, oy - z * scale
        # FOV actual (gris)
        if self._fov_current:
            L = max(span_x, span_z) * 1.2
            for sgn in (-1, 1):
                a = math.radians(self._fov_current / 2)
                c.create_line(*P(0, 0), *P(sgn * L * math.sin(a), L * math.cos(a)), fill="#5a6378", dash=(5, 4))
        # FOV calculado (verde)
        (lx, lz), (rx, rz) = res.edges
        c.create_polygon(*P(0, 0), *P(lx, lz), *P(rx, rz), fill="#173b2a", outline="")
        for x, z in res.edges:
            c.create_line(*P(0, 0), *P(x, z), fill="#4cd97b", width=2)
        # pantallas
        for poly in res.outline:
            flat = [v for p in poly for v in P(*p)]
            c.create_line(*flat, fill="#4cc9f0", width=6, capstyle="round")
        # arco del ángulo
        rr = 60
        a0 = 90 - res.fov_h / 2
        c.create_arc(ox - rr, oy - rr, ox + rr, oy + rr, start=a0, extent=res.fov_h, style="arc", outline="#4cd97b", width=2)
        c.create_text(ox, oy - rr - 14, text=f"{res.fov_h:.0f}°", fill="#e8ecf4", font=("Segoe UI", 14, "bold"))
        # ojos y cota
        c.create_oval(ox - 9, oy - 6, ox + 9, oy + 6, fill="#e8ecf4", outline="")
        c.create_text(ox, oy + 18, text="tus ojos", fill="#8a93a6")
        c.create_line(ox + 30, oy, ox + 30, P(0, s.distance)[1], fill="#8a93a6", arrow="both")
        c.create_text(ox + 36, (oy + P(0, s.distance)[1]) / 2, text=f"{s.distance:.0f} mm", fill="#8a93a6", anchor="w")

    def _fov_from_diag(self):
        try:
            t = self.fv_type.get()
            R = float(self.fv["radius"].get()) if "curvo" in t else None
            w = fov.width_from_diagonal(float(self.fv["diag"].get()), self.fv_aspect.get(), R)
            self.fv["width"].set(round(w + 2 * float(self.fv["bezel"].get() or 0)))
            self._fov_update()
        except (tk.TclError, ValueError):
            pass

    def _fov_ideal(self):
        if self._fov_result and self._fov_result.ideal_side_angle is not None:
            self.fv["angle"].set(round(self._fov_result.ideal_side_angle))
            self._fov_update()
        else:
            messagebox.showinfo(APP, "El ángulo ideal solo aplica a 3 monitores planos.")

    def _fov_load_from_ini(self, silent=False):
        if not self.cfg or not self.cfg.renderer:
            return
        s, cur = fov.setup_from_ini(self.cfg.renderer.flat(), self.cfg.app.flat() if self.cfg.app else None)
        self.fv_type.set(f"{s.monitors} {'curvo' if s.curved else 'plano'}{'s' if s.monitors == 3 else ''}")
        for k, v in (("width", s.monitor_width), ("bezel", s.bezel), ("dist", s.distance), ("angle", s.side_angle),
                     ("radius", s.radius)):
            self.fv[k].set(round(v))
        self.fv_3p.set(s.three_projections)
        self._fov_current = cur
        self._fov_update()
        if not silent:
            self.status("Valores de monitor leídos de iRacing")

    def _fov_apply(self):
        if not self.cfg or not self._fov_result:
            return
        s = self._fov_setup()
        ch = fov.ini_changes(s, self._fov_result.fov_h)
        flat_r = self.cfg.renderer.flat() if self.cfg.renderer else {}
        flat_a = self.cfg.app.flat() if self.cfg.app else {}
        changes = []
        for c in ch:
            src = flat_r if c["file"] == "renderer" else flat_a
            cur = src.get(c["key"].lower())
            if cur is None:
                continue
            try:
                same = abs(float(cur) - float(c["proposed"])) < 0.05
            except ValueError:
                same = cur == c["proposed"]
            if not same:
                changes.append({**c, "current": cur, "proposed": c["proposed"] if c["key"] != "drivingCamFOV" else f"{float(c['proposed']):.6f}"})
        if not changes:
            messagebox.showinfo(APP, "Tu configuración ya coincide.")
            return
        if self._confirm_apply(changes, "la configuración de monitor y FOV"):
            self._do_apply(changes, "fov")

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
