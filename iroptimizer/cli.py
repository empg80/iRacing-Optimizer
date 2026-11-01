"""Interfaz de línea de comandos.

  python -m iroptimizer scan                    # equipo + configuración actual
  python -m iroptimizer monitor [--analyze]     # graba una sesión (Ctrl+C para parar)
  python -m iroptimizer analyze sesion.csv      # informe HTML + recomendaciones
  python -m iroptimizer apply sesion.recs.json  # aplica cambios (con backup)
  python -m iroptimizer restore                 # deshace el último apply
  python -m iroptimizer demo                    # prueba sin iRacing
"""
from __future__ import annotations

from .i18n import _

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
    print(_("\nCarpeta: {d}\nRenderer: {r}  (candidatos: {c})", d=cfg.directory, r=cfg.renderer_name,
            c=", ".join(n for n, _mt in cfg.renderer_candidates)))
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
    print(_("\n{s}: FPS medio {m:.0f} | p5 {p5:.0f} | objetivo {t:.0f} | bajo objetivo {b:.0f} %", s=a["primary"],
            m=g["fps"]["mean"], p5=g["fps"]["p5"], t=a["target_fps"], b=g["below_target_pct"]))
    print(_("Límite: ") + ", ".join(f"{k} {v:.0f}%" for k, v in g["bottleneck"].items()))
    for r in recs:
        if r.get("key"):
            print(f"  P{r['priority']} {r['key']}: {r['current']} → {r['proposed']}")
        else:
            print(f"  P{r['priority']} {r['title']}")
    for name, ch in res.profiles.items():
        print(_("Perfil {name}: {p1} cambios", name=name, p1=len(ch)))
    print(_("\nInforme: {h}\nRecomendaciones: {j}", h=res.html_path, j=res.json_path))
    if open_browser:
        webbrowser.open(res.html_path.resolve().as_uri())
    return res.html_path, res.json_path


def cmd_monitor(args):
    from .monitor import SessionMonitor
    cfg = config_reader.load(args.ini_dir, args.renderer)
    meta = {"hardware": hardware.get_hardware(), "config": config_reader.snapshot(cfg)}
    if args.vr_hz:
        from . import vr
        meta["vr"] = {**vr.detect(), "active": True, "hz": args.vr_hz}
    pm_exe = None
    if args.presentmon:
        from . import presentmon
        pm_exe = presentmon.find_exe([_out_dir(args.out) / "tools"])
        if not pm_exe:
            print(_("PresentMon no encontrado; se graba sin frametimes."))
        elif not presentmon.can_capture():
            print(_("Aviso: sin permiso para PresentMon (grupo Performance Log Users o administrador). Se intentará igualmente."))
    mon = SessionMonitor(_out_dir(args.out), hz=args.hz, meta_extra=meta, presentmon_exe=pm_exe,
                         config_loader=lambda: config_reader.snapshot(config_reader.load(args.ini_dir, cfg.renderer_name)))
    csv_path, _meta = mon.run(duration_s=args.duration)
    print(_("\nGuardado: {p}", p=csv_path))
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


def cmd_compare(args):
    print(pipeline.compare_text(pipeline.compare(args.a, args.b)))


def cmd_export(args):
    from . import sharing
    print(sharing.export_session(Path(args.csv), args.out))


def cmd_check_update(args):
    """Consulta manual (no guarda nada ni depende del ajuste de la interfaz)."""
    from . import __version__, updates
    rel = updates.newer_release(__version__)
    if rel:
        print(_("Nueva versión {v} disponible (tienes la {cur}).", v=rel["version"], cur=__version__))
        print(rel["page_url"])
    else:
        print(_("Tienes la última versión ({cur}) o no se pudo comprobar.", cur=__version__))


def cmd_restore(args):
    for line in applier.restore(config_reader.find_iracing_dir(args.ini_dir), args.stamp):
        print(line)


def cmd_demo(args):
    from . import demo
    out = _out_dir(args.out)
    csv_path, ini_dir = demo.make(out, with_presentmon=True)
    _html, json_p = _run_analysis(csv_path, None, not args.no_browser)
    print(_("\nPrueba apply en los ini de demo:\n  python -m iroptimizer apply \"{json_p}\" --ini-dir \"{ini_dir}\" --dry-run", json_p=json_p, ini_dir=ini_dir))


def main(argv=None):
    p = argparse.ArgumentParser(prog="iroptimizer", description=_("Optimizador de gráficos y rendimiento para iRacing"))
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ini-dir", help=_("Carpeta de los .ini (por defecto Documentos\\iRacing)"))
    common.add_argument("--renderer", help="Forzar renderer (p. ej. rendererDX11OpenXR.ini)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", parents=[common]).set_defaults(fn=cmd_scan)

    m = sub.add_parser("monitor", parents=[common])
    m.add_argument("--out", help=_("Carpeta de salida (por defecto ~/iRacingOptimizer)"))
    m.add_argument("--hz", type=float, default=4.0, help=_("Muestras por segundo (4 por defecto)"))
    m.add_argument("--duration", type=float, help=_("Segundos máximos de grabación"))
    m.add_argument("--analyze", action="store_true", help=_("Analizar al terminar"))
    m.add_argument("--presentmon", action="store_true", help=_("Medir frametimes con PresentMon"))
    m.add_argument("--vr-hz", type=float, help=_("Sesión en VR: frecuencia del visor (p. ej. 90)"))
    m.add_argument("--no-browser", action="store_true")
    m.add_argument("--target", type=float, help=_("FPS objetivo (por defecto: tope de iRacing o Hz de la pantalla)"))
    m.set_defaults(fn=cmd_monitor)

    a = sub.add_parser("analyze", parents=[common])
    a.add_argument("csv")
    a.add_argument("--presentmon", help=_("CSV de PresentMon de la misma sesión"))
    a.add_argument("--no-browser", action="store_true")
    a.add_argument("--target", type=float, help=_("FPS objetivo (por defecto: tope de iRacing o Hz de la pantalla)"))
    a.set_defaults(fn=cmd_analyze)

    ap = sub.add_parser("apply", parents=[common])
    ap.add_argument("recs", help="Archivo .recs.json")
    ap.add_argument("--max-priority", type=int, default=2, choices=[1, 2, 3])
    ap.add_argument("--keys", nargs="*", help=_("Solo estas claves"))
    ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(fn=cmd_apply)

    pr = sub.add_parser("profile", parents=[common], help=_("Aplica un perfil genérico"))
    pr.add_argument("name", choices=profiles.ORDER)
    pr.add_argument("--from-session", help=_("CSV de una sesión para ajustar tope de FPS y LOD"))
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(fn=cmd_profile)

    c = sub.add_parser("compare", parents=[common], help=_("Compara dos sesiones (antes/después)"))
    c.add_argument("a", help=_("CSV de la sesión anterior"))
    c.add_argument("b", help=_("CSV de la sesión nueva"))
    c.set_defaults(fn=cmd_compare)

    ex = sub.add_parser("export", parents=[common], help=_("Exporta una sesión anonimizada (.zip) para compartir"))
    ex.add_argument("csv")
    ex.add_argument("--out")
    ex.set_defaults(fn=cmd_export)

    sub.add_parser("check-update", parents=[common], help=_("Comprueba si hay una versión nueva en GitHub")).set_defaults(fn=cmd_check_update)

    r = sub.add_parser("restore", parents=[common])
    r.add_argument("--stamp", help=_("Copia concreta (carpeta en iroptimizer_backups)"))
    r.set_defaults(fn=cmd_restore)

    d = sub.add_parser("demo", parents=[common])
    d.add_argument("--out")
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(_("Error: {e}", e=e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
