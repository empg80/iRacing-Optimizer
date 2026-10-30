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
