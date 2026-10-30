"""Interfaz de línea de comandos.

  python -m iroptimizer scan                    # equipo + configuración actual
  python -m iroptimizer monitor [--analyze]     # graba una sesión (Ctrl+C para parar)
  python -m iroptimizer analyze sesion.csv      # informe HTML + recomendaciones
  python -m iroptimizer apply sesion.recs.json  # aplica cambios (con backup)
  python -m iroptimizer restore                 # deshace el último apply
  python -m iroptimizer demo                    # prueba sin iRacing
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import apply as applier, config_reader, hardware, pipeline, profiles

KEYS_SHOWN = ["MaxCarsToDraw", "MaxCarsToDrawInMirrors", "ShadowMapType", "ShaderQuality", "MirrorDetail",
              "MaxCockpitMirrors", "NumDynamicCubemaps", "ParticleDetail", "DynamicLODSpacing", "LimitFrameRate",
              "DesiredFPSLimit", "VidMemMB", "MaxWorkingSetMB_64Bit", "NumMonitors"]


def _out_dir(p: str | None) -> Path:
    d = Path(p) if p else Path.home() / "iRacingOptimizer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_scan(args):
    hw = hardware.get_hardware()
    print(json.dumps(hw, indent=2, ensure_ascii=False))
    cfg = config_reader.load(args.ini_dir, args.renderer)
    print(f"\nCarpeta: {cfg.directory}\nRenderer: {cfg.renderer_name}  (candidatos: "
          + ", ".join(n for n, _ in cfg.renderer_candidates) + ")")
    if cfg.renderer:
        for k in KEYS_SHOWN:
            v = cfg.renderer.get(k)
            if v is not None:
                print(f"  {k:<26}{v}")
    if cfg.app and cfg.app.get("maxCars") is not None:
        print(f"  {'app.ini maxCars':<26}{cfg.app.get('maxCars')}")


def _run_analysis(csv_path: Path, presentmon: str | None, open_browser=True, target=None):
    res = pipeline.run(csv_path, presentmon, target)
    a, recs = res.analysis, res.recs
    g = a["groups"][a["primary"]]
    print(f"\n{a['primary']}: FPS medio {g['fps']['mean']:.0f} | p5 {g['fps']['p5']:.0f} | objetivo {a['target_fps']:.0f} | "
          f"bajo objetivo {g['below_target_pct']:.0f} %")
    print("Límite: " + ", ".join(f"{k} {v:.0f}%" for k, v in g["bottleneck"].items()))
    for r in recs:
        if r.get("key"):
            print(f"  P{r['priority']} {r['key']}: {r['current']} → {r['proposed']}")
        else:
            print(f"  P{r['priority']} {r['title']}")
    for name, ch in res.profiles.items():
        print(f"Perfil {name}: {len(ch)} cambios")
    print(f"\nInforme: {res.html_path}\nRecomendaciones: {res.json_path}")
    if open_browser:
        webbrowser.open(res.html_path.resolve().as_uri())
    return res.html_path, res.json_path


def cmd_monitor(args):
    from .monitor import SessionMonitor
    cfg = config_reader.load(args.ini_dir, args.renderer)
    meta = {"hardware": hardware.get_hardware(), "config": config_reader.snapshot(cfg)}
    mon = SessionMonitor(_out_dir(args.out), hz=args.hz, meta_extra=meta)
    csv_path, _ = mon.run(duration_s=args.duration)
    print(f"\nGuardado: {csv_path}")
    if args.analyze:
        _run_analysis(csv_path, None, not args.no_browser, args.target)


def cmd_analyze(args):
    _run_analysis(Path(args.csv), args.presentmon, not args.no_browser, args.target)


def cmd_apply(args):
    ini_dir = config_reader.find_iracing_dir(args.ini_dir)
    for line in applier.apply(Path(args.recs), ini_dir, args.max_priority, args.keys, args.dry_run):
        print(line)


def cmd_profile(args):
    cfg = config_reader.load(args.ini_dir, args.renderer)
    analysis = None
    if args.from_session:
        analysis = pipeline.run(Path(args.from_session), current_renderer_flat=cfg.renderer.flat()).analysis
    hw = hardware.get_hardware()
    ch = pipeline.profiles_for(cfg.renderer.flat(), hw, analysis)[args.name]
    for line in applier.apply_changes(ch, cfg.directory, cfg.renderer_name, args.dry_run, f"perfil_{args.name}"):
        print(line)


def cmd_restore(args):
    for line in applier.restore(config_reader.find_iracing_dir(args.ini_dir), args.stamp):
        print(line)


def cmd_demo(args):
    from . import demo
    out = _out_dir(args.out)
    csv_path, ini_dir = demo.make(out)
    _, json_p = _run_analysis(csv_path, None, not args.no_browser)
    print(f"\nPrueba apply en los ini de demo:\n  python -m iroptimizer apply \"{json_p}\" --ini-dir \"{ini_dir}\" --dry-run")


def main(argv=None):
    p = argparse.ArgumentParser(prog="iroptimizer", description="Optimizador de gráficos y rendimiento para iRacing")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ini-dir", help="Carpeta de los .ini (por defecto Documentos\\iRacing)")
    common.add_argument("--renderer", help="Forzar renderer (p. ej. rendererDX11OpenXR.ini)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", parents=[common]).set_defaults(fn=cmd_scan)

    m = sub.add_parser("monitor", parents=[common])
    m.add_argument("--out", help="Carpeta de salida (por defecto ~/iRacingOptimizer)")
    m.add_argument("--hz", type=float, default=4.0, help="Muestras por segundo (4 por defecto)")
    m.add_argument("--duration", type=float, help="Segundos máximos de grabación")
    m.add_argument("--analyze", action="store_true", help="Analizar al terminar")
    m.add_argument("--no-browser", action="store_true")
    m.add_argument("--target", type=float, help="FPS objetivo (por defecto: tope de iRacing o Hz de la pantalla)")
    m.set_defaults(fn=cmd_monitor)

    a = sub.add_parser("analyze", parents=[common])
    a.add_argument("csv")
    a.add_argument("--presentmon", help="CSV de PresentMon de la misma sesión")
    a.add_argument("--no-browser", action="store_true")
    a.add_argument("--target", type=float, help="FPS objetivo (por defecto: tope de iRacing o Hz de la pantalla)")
    a.set_defaults(fn=cmd_analyze)

    ap = sub.add_parser("apply", parents=[common])
    ap.add_argument("recs", help="Archivo .recs.json")
    ap.add_argument("--max-priority", type=int, default=2, choices=[1, 2, 3])
    ap.add_argument("--keys", nargs="*", help="Solo estas claves")
    ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(fn=cmd_apply)

    pr = sub.add_parser("profile", parents=[common], help="Aplica un perfil genérico")
    pr.add_argument("name", choices=profiles.ORDER)
    pr.add_argument("--from-session", help="CSV de una sesión para ajustar tope de FPS y LOD")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(fn=cmd_profile)

    r = sub.add_parser("restore", parents=[common])
    r.add_argument("--stamp", help="Copia concreta (carpeta en iroptimizer_backups)")
    r.set_defaults(fn=cmd_restore)

    d = sub.add_parser("demo", parents=[common])
    d.add_argument("--out")
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
