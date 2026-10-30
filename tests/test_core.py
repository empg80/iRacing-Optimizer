import shutil
from pathlib import Path

from iroptimizer import apply as applier, config_reader, demo, iniio, pipeline


def _demo(tmp_path):
    csv_path, ini_dir = demo.make(tmp_path)
    return csv_path, ini_dir


def test_ini_roundtrip_preserves_comments(tmp_path):
    _, ini_dir = _demo(tmp_path)
    p = ini_dir / "rendererDX11Monitor.ini"
    original = p.read_text()
    ini = iniio.IniFile(p)
    assert ini.get("maxcarstodraw") == "40"
    ini.set("MaxCarsToDraw", 20)
    ini.save()
    new = p.read_text()
    assert "MaxCarsToDraw=20" in new and "; 10 to 64" in new
    assert len(new.splitlines()) == len(original.splitlines())


def test_pipeline_and_profiles(tmp_path):
    csv_path, ini_dir = _demo(tmp_path)
    res = pipeline.run(csv_path)
    assert res.analysis["primary"] == "Race"
    assert any(r.get("key") == "MaxCarsToDrawInMirrors" for r in res.recs)
    assert set(res.profiles) == {"rendimiento", "equilibrado", "calidad"}
    caps = {n: next(c for c in ch if c["key"] == "DesiredFPSLimit")["proposed"] for n, ch in res.profiles.items()}
    assert int(caps["rendimiento"]) >= int(caps["equilibrado"]) >= int(caps["calidad"])
    assert all(int(v) <= 237 for v in caps.values())
    assert res.html_path.exists() and res.json_path.exists()


def test_apply_and_restore(tmp_path):
    csv_path, ini_dir = _demo(tmp_path)
    res = pipeline.run(csv_path)
    p1 = [r for r in res.recs if r.get("key") and r["priority"] == 1]
    log = applier.apply_changes(p1, ini_dir, "rendererDX11Monitor.ini", label="test")
    assert log[0].startswith("Copia de seguridad")
    cfg = config_reader.load(str(ini_dir))
    assert cfg.renderer.get("LimitFrameRate") == "1"
    backups = applier.list_backups(ini_dir)
    assert len(backups) == 1
    applier.restore(ini_dir, backups[0].name)
    assert config_reader.load(str(ini_dir)).renderer.get("LimitFrameRate") == "0"


def test_unknown_keys_are_skipped(tmp_path):
    _, ini_dir = _demo(tmp_path)
    log = applier.apply_changes([{"file": "renderer", "key": "NoExiste", "proposed": "1"}],
                                ini_dir, "rendererDX11Monitor.ini", dry_run=True)
    assert "no existe" in log[1]


def test_compare_detects_setting_changes(tmp_path):
    csv_a, ini_dir = _demo(tmp_path / "a")
    csv_b, _ = _demo(tmp_path / "b")
    import json
    meta = json.loads(csv_b.with_suffix(".json").read_text())
    meta["config"]["renderer"]["Graphics Options"]["MaxCarsToDraw"] = "20"
    csv_b.with_suffix(".json").write_text(json.dumps(meta))
    c = pipeline.compare(csv_a, csv_b)
    assert {"key": "maxcarstodraw", "a": "40", "b": "20"} in c["changed_settings"]
    assert "MaxCarsToDraw".lower() in pipeline.compare_text(c)


def test_diagnostics_and_live_watch(tmp_path):
    from iroptimizer import diagnostics
    csv_path, _ = _demo(tmp_path)
    res = pipeline.run(csv_path)
    d = res.diagnostics
    areas = {c["area"] for c in d["checks"]}
    assert {"CPU", "GPU", "Frametime", "Red", "Memoria"} <= areas
    assert all(c["status"] in ("ok", "aviso", "problema", "info") for c in d["checks"])
    w = diagnostics.LiveWatch()
    msgs = []
    for i in range(10):
        msgs += w.feed({"on_track": 1, "t": i, "fps": 150, "chan_latency": 0.05})
    msgs += w.feed({"on_track": 1, "t": 11, "fps": 70, "chan_latency": 0.3, "chan_quality": 0.8})
    assert any("Tirón" in m for m in msgs) and any("latencia" in m for m in msgs)
    assert any("paquetes" in m for m in msgs)


def test_fov_geometry():
    from iroptimizer import fov
    # 27" 16:9 a 60 cm ≈ 53° (referencia habitual)
    r = fov.compute(fov.Setup(monitor_width=fov.width_from_diagonal(27), bezel=0, distance=600))
    assert 52 <= r.fov_h <= 54
    # curvo: más FOV que el plano equivalente
    flat = fov.compute(fov.Setup(monitor_width=1156, bezel=4, distance=540)).fov_h
    curved = fov.compute(fov.Setup(curved=True, monitor_width=1156, bezel=4, distance=540, radius=1000)).fov_h
    assert curved > flat
    # triples: ángulo ideal razonable y FOV total mayor que el central
    t = fov.compute(fov.Setup(monitors=3, monitor_width=640, bezel=10, distance=650, side_angle=50))
    assert 40 < t.ideal_side_angle < 65 and t.fov_h > t.fov_center * 2


def test_user_profiles_roundtrip(tmp_path):
    from iroptimizer import userprofiles
    csv_path, ini_dir = _demo(tmp_path)
    base = tmp_path / "out"
    userprofiles.save(base, "Monitor", ini_dir, "rendererDX11Monitor.ini", ["graficos"])
    ini = iniio.IniFile(ini_dir / "rendererDX11Monitor.ini")
    ini.set("MaxCarsToDraw", 12)
    ini.save()
    prof = userprofiles.list_profiles(base)[0]
    ch = userprofiles.changes_for(prof, ini_dir)
    assert [c["key"] for c in ch] == ["MaxCarsToDraw"] and ch[0]["proposed"] == "40"
    applier.apply_changes(ch, ini_dir, prof["renderer_file"], label="t")
    assert iniio.IniFile(ini_dir / "rendererDX11Monitor.ini").get("MaxCarsToDraw") == "40"


def test_presentmon_analysis(tmp_path):
    csv_path, _ = demo.make(tmp_path, with_presentmon=True)
    res = pipeline.run(csv_path)
    pm = res.analysis["presentmon"]
    assert pm and pm["aligned"] and pm["frames"] > 10000
    assert pm["low1_fps"] < pm["avg_fps"] and pm["stutter_events"]
    assert any(c["title"].startswith("Tirones reales") for c in res.diagnostics["checks"])
    assert any(e["kind"].startswith("tirón (") and e["session_type"] == "Race" for e in res.diagnostics["events"])


def test_presentmon_column_aliases(tmp_path):
    from iroptimizer import presentmon
    p = tmp_path / "pm.csv"
    p.write_text("Application,TimeInSeconds,MsBetweenPresents\n" +
                 "".join(f"iRacingSim64DX11.exe,{i/100},{10 if i % 50 else 40}\n" for i in range(500)) +
                 "otro.exe,1,5\n")
    fr = presentmon.load_frames(p)
    assert len(fr) == 500 and fr[1][1] == 10
    a = presentmon.analyze(fr)
    assert a["stutters_over_2x"] >= 5 and 90 < a["avg_fps"] < 100


def test_drift_detection(tmp_path):
    from iroptimizer import drift
    _, ini_dir = _demo(tmp_path)
    base = tmp_path / "out"
    applier.apply_changes([{"file": "renderer", "key": "MaxCarsToDraw", "proposed": "22", "section": "Graphics Options"},
                           {"file": "renderer", "key": "NoExiste", "proposed": "1"}],
                          ini_dir, "rendererDX11Monitor.ini", state_base=base)
    assert drift.check(base, ini_dir) == []
    ini = iniio.IniFile(ini_dir / "rendererDX11Monitor.ini")
    ini.set("MaxCarsToDraw", 40)  # simula que iRacing lo resetea
    ini.save()
    d = drift.check(base, ini_dir)
    assert len(d) == 1 and d[0]["expected"] == "22" and d[0]["current"] == "40"
    for fname, ch in drift.reapply_changes(d).items():
        applier.apply_changes(ch, ini_dir, fname, state_base=base)
    assert drift.check(base, ini_dir) == []
    ini = iniio.IniFile(ini_dir / "rendererDX11Monitor.ini")
    ini.set("MaxCarsToDraw", 30)
    ini.save()
    drift.accept_current(base, ini_dir)
    assert drift.check(base, ini_dir) == []


def test_i18n_catalog_complete_and_consistent():
    import ast
    import glob
    import string
    from iroptimizer.locale_en import CATALOG

    def names(s):
        return sorted({f[1] for f in string.Formatter().parse(s) if f[1] is not None})
    used = set()
    for f in glob.glob("iroptimizer/*.py"):
        for n in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_" and n.args and isinstance(n.args[0], ast.Constant):
                used.add(n.args[0].value)
    missing = [u for u in used if u not in CATALOG]
    assert not missing, missing[:5]
    bad = [k for k, v in CATALOG.items() if names(k) != names(v)]
    assert not bad, bad[:5]


def test_english_report(tmp_path, monkeypatch):
    from iroptimizer import i18n
    monkeypatch.setattr(i18n, "LANG", "en")
    csv_path, _ = _demo(tmp_path)
    res = pipeline.run(csv_path)
    html = res.html_path.read_text(encoding="utf-8")
    assert "Configuration recommendations" in html and 'lang="en"' in html
    assert any("Physics and network thread" in c["title"] for c in res.diagnostics["checks"])
