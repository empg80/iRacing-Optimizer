"""Tests de seguridad: entradas externas (perfiles compartidos, sesiones de otros) no deben poder
inyectar contenido en los .ini, escribir fuera de la carpeta de iRacing ni inyectar HTML."""
import json

import pytest

from iroptimizer import apply as applier, demo, iniio, pipeline, userprofiles
from iroptimizer.safety import UnsafeInput, redact_path


def test_ini_rejects_injection(tmp_path):
    _, ini_dir = demo.make(tmp_path)
    ini = iniio.IniFile(ini_dir / "rendererDX11Monitor.ini")
    for bad in ("1\n[Evil]\nx=1", "1;comentario", "a=b", "[x]", "x" * 100):
        with pytest.raises(UnsafeInput):
            ini.set("MaxCarsToDraw", bad)


def test_imported_profile_is_sanitized(tmp_path):
    _, ini_dir = demo.make(tmp_path)
    evil = {"name": "../../malo", "renderer_file": "..\\..\\Windows\\win.ini",
            "data": {"renderer": {"Graphics Options": {"MaxCarsToDraw": "1\n[Evil]\nx=1", "MirrorDetail": "0"}}}}
    src = tmp_path / "evil.json"
    src.write_text(json.dumps(evil))
    dst = userprofiles.import_file(tmp_path / "out", src)
    assert dst.parent == (tmp_path / "out" / "perfiles")
    prof = userprofiles.list_profiles(tmp_path / "out")[0]
    assert prof["renderer_file"] == "rendererDX11Monitor.ini"
    ch = userprofiles.changes_for(prof, ini_dir)
    assert [c["key"] for c in ch] == ["MirrorDetail"]


def test_apply_rejects_foreign_files(tmp_path):
    _, ini_dir = demo.make(tmp_path)
    with pytest.raises(UnsafeInput):
        applier.apply_changes([{"file": "renderer", "key": "x", "proposed": "1"}], ini_dir, "../../evil.ini")
    log = applier.apply_changes([{"file": "otro", "key": "x", "proposed": "1"}], ini_dir, None, dry_run=True)
    assert "no permitido" in log[1]


def test_restore_rejects_path_traversal(tmp_path):
    _, ini_dir = demo.make(tmp_path)
    applier.backup(ini_dir, "x")
    with pytest.raises(UnsafeInput):
        applier.restore(ini_dir, "../../..")


def test_report_escapes_untrusted_session_text(tmp_path):
    csv_path, _ = demo.make(tmp_path)
    meta = json.loads(csv_path.with_suffix(".json").read_text())
    meta["track"] = "<script>alert(1)</script>"
    csv_path.with_suffix(".json").write_text(json.dumps(meta))
    html = pipeline.run(csv_path).html_path.read_text(encoding="utf-8")
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_redact_path():
    import os
    home = os.path.expanduser("~")
    assert redact_path(os.path.join(home, "Documents", "iRacing")).startswith("%USERPROFILE%")


def test_export_is_anonymized(tmp_path):
    import zipfile
    from iroptimizer import sharing
    csv_path, _ = demo.make(tmp_path, with_presentmon=True)
    meta = json.loads(csv_path.with_suffix(".json").read_text())
    meta["top_processes"] = [{"name": "MiProgramaSecreto.exe", "cpu_pct": 12.0}]
    meta["config"]["app"]["Audio"] = {"devSpeakerName": "Auriculares de Kike"}
    meta["config"]["directory"] = "C:\\Users\\kike\\Documents\\iRacing"
    csv_path.with_suffix(".json").write_text(json.dumps(meta))
    z = sharing.export_session(csv_path)
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        blob = zf.read("session_demo.json").decode()
    assert names == {"session_demo.csv", "session_demo.json", "session_demo.presentmon.csv"}
    assert "MiProgramaSecreto" not in blob and "Auriculares" not in blob and "kike" not in blob
    assert json.loads(blob)["background_cpu_pct"] == 12.0
    # e importarla de nuevo funciona y se puede analizar
    out = sharing.import_session(z, tmp_path / "rec")
    assert out.name == "session_demo_recibida.csv"
    assert pipeline.run(out).analysis["presentmon"]


def test_import_rejects_malicious_zip(tmp_path):
    import zipfile
    from iroptimizer import sharing
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../../evil.csv", "x")
    with pytest.raises(ValueError):
        sharing.import_session(bad, tmp_path / "rec")
    ok = tmp_path / "extra.zip"
    with zipfile.ZipFile(ok, "w") as z:
        z.writestr("session_a.csv", "t,fps\n")
        z.writestr("session_a.json", "{}")
        z.writestr("malware.exe", "MZ")
    sharing.import_session(ok, tmp_path / "rec2")
    assert not (tmp_path / "rec2" / "malware.exe").exists()
    assert sorted(p.name for p in (tmp_path / "rec2").iterdir()) == ["session_a_recibida.csv", "session_a_recibida.json"]


def test_update_check_parsing_and_urls(monkeypatch):
    from iroptimizer import updates
    assert updates.parse_version("v0.9.0") == (0, 9, 0) == updates.parse_version("0.9.0")
    assert updates.parse_version("0.9.0-beta") is None and updates.parse_version("../x") is None
    good = {"tag_name": "0.9.0", "html_url": "https://github.com/empg80/iRacing-Optimizer/releases/tag/0.9.0",
            "assets": [{"name": "iRacingOptimizer.exe",
                        "browser_download_url": "https://github.com/empg80/iRacing-Optimizer/releases/download/0.9.0/iRacingOptimizer.exe"}]}
    rel = updates.parse_release(good)
    assert rel["version"] == "0.9.0" and rel["download_url"].endswith("/iRacingOptimizer.exe")
    evil = dict(good, html_url="https://evil.example/x",
                assets=[{"name": "iRacingOptimizer.exe", "browser_download_url": "https://evil.example/a.exe"}])
    rel = updates.parse_release(evil)
    assert rel["page_url"] == updates.RELEASES_URL and rel["download_url"] == updates.RELEASES_URL
    assert updates.parse_release(dict(good, prerelease=True)) is None

    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=5: updates.parse_release(good))
    assert updates.newer_release("0.8.0")["version"] == "0.9.0"
    assert updates.newer_release("0.9.0") is None
    assert updates.newer_release("0.8.0", {"update_skip_version": "0.9.0"}) is None

    def boom(timeout=5):
        raise OSError("sin red")
    monkeypatch.setattr(updates, "fetch_latest", boom)
    assert updates.newer_release("0.8.0") is None


def test_update_check_off_by_default(monkeypatch):
    from iroptimizer import updates
    monkeypatch.delenv("IROPT_NO_UPDATE_CHECK", raising=False)
    assert not updates.should_check({})                      # sin preguntar = desactivado
    assert not updates.should_check({"update_check": False})
    assert updates.should_check({"update_check": True}, now=10 ** 9)
    assert not updates.should_check({"update_check": True, "update_last_check": 10 ** 9 - 60}, now=10 ** 9)
    monkeypatch.setenv("IROPT_NO_UPDATE_CHECK", "1")
    assert not updates.should_check({"update_check": True}, now=10 ** 9)
