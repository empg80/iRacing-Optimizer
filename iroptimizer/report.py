"""Informe HTML autocontenido (sin dependencias ni CDN) + JSON de recomendaciones."""
from __future__ import annotations

import html
import json
from pathlib import Path

CAT = {"cpu": "CPU (hilo principal)", "gpu": "GPU", "frametime": "Frametime", "memoria": "Memoria",
       "red": "Red", "sistema": "Sistema", "calidad": "Subir calidad"}
BN = {"cap": "En el tope", "gpu": "GPU", "cpu": "CPU / motor", "mixed": "Mixto", "other": "Sin datos GPU"}
COL = {"fps": "#4cc9f0", "gpu": "#f72585", "cpu_fg": "#ffb703", "target": "#8ac926"}


def _e(x):
    return html.escape(str(x))


def _n(x, d=0, suf=""):
    return "–" if x is None else f"{x:.{d}f}{suf}"


def _line_chart(series, keys, y_max, target=None, w=960, h=260, right_keys=()):
    pad_l, pad_r, pad_t, pad_b = 44, 44, 14, 26
    if not series:
        return ""
    xs = [p["t"] for p in series]
    x0, x1 = xs[0], xs[-1] or 1
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def X(t):
        return pad_l + (t - x0) / ((x1 - x0) or 1) * iw

    def Y(v, top):
        return pad_t + ih - min(v, top) / top * ih

    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">']
    for i in range(5):
        v = y_max * i / 4
        y = Y(v, y_max)
        out.append(f'<line x1="{pad_l}" x2="{w-pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{pad_l-6}" y="{y+4:.1f}" class="ax" text-anchor="end">{v:.0f}</text>')
        if right_keys:
            out.append(f'<text x="{w-pad_r+6}" y="{y+4:.1f}" class="ax">{25*i}%</text>')
    for i in range(6):
        t = x0 + (x1 - x0) * i / 5
        out.append(f'<text x="{X(t):.1f}" y="{h-6}" class="ax" text-anchor="middle">{t:.0f}′</text>')
    if target:
        y = Y(target, y_max)
        out.append(f'<line x1="{pad_l}" x2="{w-pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="{COL["target"]}" stroke-dasharray="5 4"/>')
    for k in keys:
        top = 100 if k in right_keys else y_max
        pts = " ".join(f"{X(p['t']):.1f},{Y(p[k], top):.1f}" for p in series if p.get(k) is not None)
        if pts:
            width = 1.8 if k == "fps" else 1.1
            op = 1 if k == "fps" else 0.75
            out.append(f'<polyline points="{pts}" fill="none" stroke="{COL[k]}" stroke-width="{width}" opacity="{op}"/>')
    out.append("</svg>")
    return "".join(out)


def _bars(traffic, target, w=480, h=220):
    if not traffic:
        return "<p class='muted'>Sin datos de tráfico.</p>"
    pad_l, pad_b, pad_t = 40, 34, 10
    top = max(max(t["fps_mean"] for t in traffic), target or 0) * 1.1
    bw = (w - pad_l - 10) / len(traffic)
    ih = h - pad_b - pad_t
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">']
    if target:
        y = pad_t + ih - target / top * ih
        out.append(f'<line x1="{pad_l}" x2="{w-10}" y1="{y:.1f}" y2="{y:.1f}" stroke="{COL["target"]}" stroke-dasharray="5 4"/>')
    for i, t in enumerate(traffic):
        bh = t["fps_mean"] / top * ih
        x = pad_l + i * bw + bw * 0.15
        y = pad_t + ih - bh
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*0.7:.1f}" height="{bh:.1f}" rx="3" fill="{COL["fps"]}"/>'
                   f'<text x="{x+bw*0.35:.1f}" y="{y-4:.1f}" class="ax" text-anchor="middle">{t["fps_mean"]:.0f}</text>'
                   f'<text x="{x+bw*0.35:.1f}" y="{h-18}" class="ax" text-anchor="middle">{_e(t["bucket"])}</text>'
                   f'<text x="{x+bw*0.35:.1f}" y="{h-5}" class="ax muted" text-anchor="middle">n={t["n"]}</text>')
    out.append("</svg>")
    return "".join(out)


CSS = """
body{background:#0f1115;color:#e6e6e6;font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px;max-width:1080px;margin:auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;border-bottom:1px solid #2a2f3a;padding-bottom:6px}
.muted{color:#8a93a6}.grid{stroke:#232836}.ax{fill:#8a93a6;font-size:11px}.chart{width:100%;height:auto;background:#151922;border-radius:8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:#151922;border-radius:8px;padding:10px 12px}.kpi b{display:block;font-size:22px}.kpi span{color:#8a93a6;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid #232836;text-align:left;vertical-align:top}
th{color:#8a93a6;font-weight:500}.p1{color:#ff6b6b}.p2{color:#ffb703}.p3{color:#8a93a6}code{background:#1d2230;padding:1px 5px;border-radius:4px}
.legend span{margin-right:14px;font-size:13px}.legend i{display:inline-block;width:12px;height:3px;margin-right:5px;vertical-align:middle}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:800px){.two{grid-template-columns:1fr}}
.kpi li b{display:inline;font-size:inherit}
.pill{color:#fff;font-size:11px;font-weight:700;padding:2px 7px;border-radius:10px;white-space:nowrap}
td ul{margin:4px 0 0;padding-left:18px;color:#c9cfdb}
.bar{display:flex;height:16px;border-radius:4px;overflow:hidden;margin:6px 0}.bar div{height:100%}
"""

BN_COL = {"cap": "#8ac926", "gpu": "#f72585", "cpu": "#ffb703", "mixed": "#fb8500", "other": "#5a6378"}


def render_html(a: dict, recs: list[dict], profs: dict | None = None, diag: dict | None = None) -> str:
    g = a["groups"][a["primary"]]
    f = g["fps"]
    hw = a["hardware"]
    cpu = (hw.get("cpu") or {}).get("name", "?")
    gpu = ((hw.get("gpus") or [{}])[0]).get("name", "?")
    disp = hw.get("display") or {}
    meta = a["meta"]
    title = " · ".join(str(x) for x in (meta.get("track"), meta.get("car")) if x) or "Sesión"

    kpi = lambda v, l: f'<div class="kpi"><b>{v}</b><span>{l}</span></div>'
    kpis = "".join([
        kpi(_n(f["mean"]), f"FPS medio ({_e(a['primary'])})"),
        kpi(_n(f["p5"]), "FPS p5 (5 % peor)"),
        kpi(_n(f["p1"]), "FPS p1"),
        kpi(_n(a["target_fps"]), "Objetivo" + (" (tope)" if a["capped"] else " (Hz pantalla)")),
        kpi(_n(g["below_target_pct"], 0, " %"), "Tiempo bajo objetivo"),
        kpi(_n(a.get("traffic_drop_pct"), 0, " %"), "Pérdida con tráfico"),
    ])
    bar = "".join(f'<div style="width:{g["bottleneck"][k]:.1f}%;background:{BN_COL[k]}" title="{BN[k]} {g["bottleneck"][k]:.0f}%"></div>'
                  for k in BN)
    bn_leg = " ".join(f'<span><i style="background:{BN_COL[k]}"></i>{BN[k]} {g["bottleneck"][k]:.0f} %</span>' for k in BN)

    groups_rows = "".join(
        f"<tr><td>{_e(k)}</td><td>{_n(s['duration_min'],1)} min</td><td>{_n(s['fps']['mean'])}</td><td>{_n(s['fps']['p5'])}</td>"
        f"<td>{_n(s['fps']['p1'])}</td><td>{_n(s['cpu_fg'].get('p95'))} %</td><td>{_n(s['gpu'].get('p95'))} %</td></tr>"
        for k, s in a["groups"].items())

    drops = "".join(
        f"<tr><td>{_e(d['session_type'])}</td><td>{_n((d['session_time'] or 0)/60,1)}′</td><td>{d['duration_s']} s</td>"
        f"<td>{_n(d['min_fps'])}</td><td>{d['cars_near_max']}</td><td>{BN.get(d['bottleneck'], d['bottleneck'])}</td></tr>"
        for d in a["drops"]) or "<tr><td colspan=6 class=muted>Sin caídas relevantes.</td></tr>"

    rec_rows = []
    for r in recs:
        if r.get("key"):
            what = (f"<code>{_e(r['key'])}</code> <span class=muted>[{_e(r.get('section') or '')}] {_e(r.get('file_name') or '')}</span>"
                    f"<br>{_e(r['current'])} → <b>{_e(r['proposed'])}</b>")
        else:
            what = f"<b>{_e(r['title'])}</b>"
        rec_rows.append(f"<tr><td class='p{r['priority']}'>P{r['priority']}</td><td>{_e(CAT.get(r['category'], r['category']))}</td>"
                        f"<td>{what}</td><td>{_e(r['reason'])}</td></tr>")
    recs_html = "".join(rec_rows) or "<tr><td colspan=4 class=muted>Nada que cambiar: vas sobrado.</td></tr>"

    zones = a.get("slow_zones") or []
    zones_html = ""
    if zones:
        zones_html = ("<h2>Zonas más lentas del circuito</h2><table><tr><th>Tramo (% vuelta)</th><th>Metros desde meta</th>"
                      "<th>FPS medio</th><th>Coches cerca (media)</th></tr>" + "".join(
            f"<tr><td>{z['from_pct']:.1f}–{z['to_pct']:.1f} %</td><td>{z['track_m']:.0f} m</td><td>{z['fps_mean']:.0f}</td>"
            f"<td>{z['cars_near_mean']:.1f}</td></tr>" for z in zones) + "</table>")
    diag_html = ""
    if diag:
        pill = {"ok": ("#2e7d4f", "OK"), "aviso": ("#b9770e", "AVISO"), "problema": ("#c0392b", "PROBLEMA"), "info": ("#1f618d", "INFO")}
        rows_ = []
        for c in diag["checks"]:
            col, lab = pill[c["status"]]
            acts = "".join(f"<li>{_e(x)}</li>" for x in c["actions"])
            rows_.append(f"<tr><td><span class=pill style='background:{col}'>{lab}</span></td><td>{_e(c['area'])}</td>"
                         f"<td><b>{_e(c['title'])}</b><br><span class=muted>{_e(c['metric'])}</span></td>"
                         f"<td>{_e(c['detail'])}" + (f"<ul>{acts}</ul>" if acts else "") + "</td></tr>")
        sm = diag.get("summary", {})
        head = " · ".join(f"{sm.get(k, 0)} {t}" for k, t in (("problema", "problemas"), ("aviso", "avisos"), ("ok", "correctos")))
        ev = diag.get("events") or []
        ev_html = ""
        if ev:
            ev_html = ("<h3 style='font-size:15px;margin:18px 0 6px'>Incidentes</h3><table><tr><th>Sesión</th><th>Minuto</th>"
                       "<th>Vuelta</th><th>Tipo</th><th>Duración</th><th>Detalle</th></tr>" + "".join(
                f"<tr><td>{_e(e.get('session_type') or '')}</td><td>{(e.get('session_time') or 0) / 60:.1f}′</td>"
                f"<td>{'' if e.get('lap_pct') is None else f'{e['lap_pct'] * 100:.0f} %'}</td><td>{_e(e['kind'])}</td>"
                f"<td>{e['duration_s']} s</td><td>{_e(e['detail'])}</td></tr>" for e in ev[:40]) + "</table>")
        diag_html = (f"<h2>Diagnóstico <span class=muted style='font-size:13px;font-weight:400'>{head}</span></h2>"
                     "<table><tr><th></th><th>Área</th><th>Comprobación</th><th>Qué significa y qué hacer</th></tr>"
                     + "".join(rows_) + "</table>" + ev_html)
    prof_html = ""
    if profs:
        from .profiles import PROFILES
        cols = []
        for name, ch in profs.items():
            items = "".join(f"<li><code>{_e(c['key'])}</code> {_e(c['current'])} → <b>{_e(c['proposed'])}</b></li>" for c in ch) \
                or "<li class=muted>Ya coincide con tu configuración.</li>"
            cols.append(f"<div class=kpi><b style='font-size:16px'>{_e(PROFILES[name]['title'])}</b>"
                        f"<span>{_e(PROFILES[name]['desc'])}</span><ul style='padding-left:18px;font-size:13px'>{items}</ul></div>")
        prof_html = "<h2>Perfiles</h2><div class=kpis style='grid-template-columns:repeat(auto-fit,minmax(280px,1fr))'>" + "".join(cols) + "</div>"
    m, n, pm = a["memory"], a["network"], a.get("presentmon")
    pm_html = ""
    if pm:
        pm_html = (f"<h2>Frametimes (PresentMon)</h2><div class=kpis>{kpi(_n(pm['avg_fps']), 'FPS medio real')}"
                   f"{kpi(_n(pm['low1_fps']), '1 % low')}{kpi(_n(pm['low01_fps']), '0,1 % low')}"
                   f"{kpi(_n(pm['frametime_max_ms'],1,' ms'), 'Frametime máx.')}{kpi(pm['stutters_over_2x'], 'Tirones (frames > 2× mediana)')}"
                   f"{kpi(_n(pm.get('stutters_per_min'),1,'/min'), 'Tirones por minuto')}"
                   + (f"{kpi(_n(pm['bound_pct']['cpu'],0,' %'), 'Frames limitados por CPU')}" if pm.get('bound_pct') else "")
                   + "</div>")

    return f"""<!doctype html><html lang="es"><meta charset="utf-8"><title>iRacing Optimizer – {_e(title)}</title>
<style>{CSS}</style><body>
<h1>{_e(title)}</h1>
<div class="muted">{_e(meta.get('started') or '')} · {_e(cpu)} · {_e(gpu)} · {_e(disp.get('width','?'))}×{_e(disp.get('height','?'))} @ {_e(disp.get('refresh','?'))} Hz · {_e(a.get('renderer_file') or '')}</div>
<h2>Resumen ({_e(a['primary'])})</h2><div class="kpis">{kpis}</div>
<p style="margin:14px 0 0">¿Qué te limita?</p><div class="bar">{bar}</div><div class="legend">{bn_leg}</div>
{diag_html}
<h2>Recomendaciones de configuración</h2>
<table><tr><th>Prio</th><th>Área</th><th>Cambio</th><th>Por qué</th></tr>{recs_html}</table>
<p class="muted">Aplica con <code>python -m iroptimizer apply &lt;archivo&gt;.recs.json --max-priority 2</code> con iRacing cerrado. Se crea copia de seguridad.</p>
{prof_html}
<h2>Evolución</h2>
<div class="legend"><span><i style="background:{COL['fps']}"></i>FPS</span><span><i style="background:{COL['gpu']}"></i>GPU %</span>
<span><i style="background:{COL['cpu_fg']}"></i>CpuUsageFG (SDK)</span><span><i style="background:{COL['target']}"></i>Objetivo</span></div>
{_line_chart(a['series'], ['gpu','cpu_fg','fps'], max(f['max'], a['target_fps']) * 1.1, a['target_fps'], right_keys=('gpu','cpu_fg'))}
<div class="two"><div><h2>FPS según coches a &lt;150 m</h2>{_bars(a['traffic'], a['target_fps'])}</div>
<div><h2>Memoria, red y temperatura</h2><table>
<tr><td>VRAM usada máx. / total</td><td>{_n(m['vram_used_max_mb'])} / {_n(m['vram_total_mb'])} MB</td></tr>
<tr><td>VRAM asignada a iRacing</td><td>{_n(m['vidmem_cfg_mb'])} MB</td></tr>
<tr><td>RAM iRacing máx. / asignada</td><td>{_n(m['ir_rss_max_mb'])} / {_n(m['maxworkingset_cfg_mb'])} MB</td></tr>
<tr><td>Fallos de página p95</td><td>{_n(m['page_faults_p95'])}/s</td></tr>
<tr><td>Calidad conexión media / p5</td><td>{_n(n['quality_mean'])} / {_n(n['quality_p5'])} %</td></tr>
<tr><td>Latencia p95</td><td>{_n(n['latency_p95_ms'])} ms</td></tr>
<tr><td>GPU temp. máx. / potencia máx.</td><td>{_n(a['thermal']['gpu_temp_max'])} °C / {_n(a['thermal']['gpu_power_max_w'])} W</td></tr>
</table></div></div>
{zones_html}
{pm_html}
<h2>Por tipo de sesión</h2><table><tr><th>Sesión</th><th>Duración</th><th>FPS medio</th><th>p5</th><th>p1</th><th>CPU FG p95</th><th>GPU p95</th></tr>{groups_rows}</table>
<h2>Peores caídas</h2><table><tr><th>Sesión</th><th>Minuto</th><th>Duración</th><th>FPS mín.</th><th>Coches cerca</th><th>Límite</th></tr>{drops}</table>
<p class="muted">FPS del SDK = media de 1 s: para 1 % lows reales importa un CSV de PresentMon con <code>--presentmon</code>. Clasificación heurística con la carga real de GPU (NVML): ≥ 90 % limita la GPU, &lt; 80 % la GPU espera → limita CPU/motor.</p>
</body></html>"""


def write(a: dict, recs: list[dict], base: Path, profs: dict | None = None,
          diag: dict | None = None) -> tuple[Path, Path]:
    html_path = base.with_suffix(".report.html")
    json_path = base.with_suffix(".recs.json")
    html_path.write_text(render_html(a, recs, profs, diag), encoding="utf-8")
    json_path.write_text(json.dumps({"renderer_file": a.get("renderer_file"), "recommendations": recs,
                                     "profiles": profs or {}, "diagnostics": diag or {}},
                                    indent=2, ensure_ascii=False), encoding="utf-8")
    return html_path, json_path
