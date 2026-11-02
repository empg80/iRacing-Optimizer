"""Regresión con sesiones reales (anonimizadas) del autor. Cada test fija algo que falló en uso real.

Las sesiones están en tests/fixtures/real (generadas con tests/fixtures/build_real.py).
"""
from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from iroptimizer import pipeline

REAL = Path(__file__).parent / "fixtures" / "real"


def _load(stem: str, tmp_path: Path):
    for gz in REAL.glob(f"{stem}.*.gz"):
        with gzip.open(gz, "rb") as fi, open(tmp_path / gz.name[:-3], "wb") as fo:
            shutil.copyfileobj(fi, fo)
    return pipeline.run(tmp_path / f"{stem}.csv")


def _keys(res):
    return {r["key"]: r["proposed"] for r in res.recs if r.get("key")}


def _check(res, title):
    return next((c for c in res.diagnostics["checks"] if c["title"].startswith(title)), None)


@pytest.fixture(scope="module")
def lemans(tmp_path_factory):  # 31-oct: Le Mans, Cadillac GTP, 50 coches, emitiendo con OBS
    return _load("session_20261031_193741", tmp_path_factory.mktemp("lemans"))


@pytest.fixture(scope="module")
def vr_monza(tmp_path_factory):  # 1-nov: Quest 3 por Air Link, visor a 72 Hz aunque se eligió 90
    return _load("session_20261101_135430", tmp_path_factory.mktemp("vr"))


@pytest.fixture(scope="module")
def hungaroring(tmp_path_factory):  # 1-nov: monitor, PresentMon con +1 h, vr_check=True erróneo (v0.9.3)
    return _load("session_20261101_183045", tmp_path_factory.mktemp("hu"))


@pytest.fixture(scope="module")
def monza120(tmp_path_factory):  # 1-nov: tope cambiado de 80 a 120 durante la grabación
    return _load("session_20261101_194811", tmp_path_factory.mktemp("mz"))


def test_lemans_cpu_bound_and_streaming(lemans):
    a = lemans.analysis
    g = a["groups"][a["primary"]]
    assert a["primary"] == "Race"
    assert g["bottleneck"]["cpu"] >= 95
    assert _keys(lemans).get("GrandstandDetail") == "0"
    # Equilibrado no sube lo que carga la CPU cuando vas limitado por CPU
    eq = {c["key"] for c in lemans.profiles["equilibrado"]}
    assert not eq & {"MaxCarsToDraw", "CrowdDetail", "PitObjectDetail", "ShadowMapType"}
    bg = _check(lemans, "Carga de CPU en segundo plano")
    assert "emitiendo" in bg["title"] and bg["status"] == "info"
    assert _check(lemans, "Latencia con el servidor")["status"] == "info"  # alta pero estable


def test_vr_real_headset_rate(vr_monza):
    v = vr_monza.analysis["vr"]
    assert v["hz_detected"] == 72 and v["hz"] == 72 and v["at_rate_pct"] >= 95
    g = vr_monza.analysis["groups"][vr_monza.analysis["primary"]]
    assert g["bottleneck"]["cpu"] < 5  # ir al ritmo del visor no es «limitado por CPU»
    assert vr_monza.analysis["renderer_mismatch"] == "vr_session_monitor_ini"
    assert _check(vr_monza, "Latencia con el servidor")["status"] == "ok"


def test_hungaroring_monitor_with_presentmon(hungaroring):
    a = hungaroring.analysis
    assert a["vr"] is None and a["renderer_mismatch"] is None  # vr_check=True de la v0.9.3 se ignora
    pm = a["presentmon"]
    assert pm and pm["frames"] > 100_000  # el desfase horario de PresentMon se alinea
    assert 70 <= pm["low1_fps"] <= 80
    assert int(float(_keys(hungaroring)["DesiredFPSLimit"])) > 80  # clavado en 80 con margen: subir


def test_monza_cap_changed_mid_session(monza120):
    a = monza120.analysis
    assert a["cap_inferred"] == 120 and a["target_fps"] == 120
    pm = a["presentmon"]
    assert pm["low1_fps"] > 95  # solo carrera: sin los minutos a 80 de prácticas
    assert _check(monza120, "Tope de FPS cambiado")
    up = int(float(_keys(monza120)["DesiredFPSLimit"]))
    assert 120 < up <= 168  # subida en pasos (máx. +40 %)


def test_presentmon_problems_are_reported(tmp_path):
    for gz in REAL.glob("session_20261101_183045.*.gz"):
        if "presentmon" in gz.name:
            continue  # sin el archivo de PresentMon
        with gzip.open(gz, "rb") as fi, open(tmp_path / gz.name[:-3], "wb") as fo:
            shutil.copyfileobj(fi, fo)
    res = pipeline.run(tmp_path / "session_20261101_183045.csv")
    c = _check(res, "PresentMon sin datos")
    assert c and c["status"] == "aviso" and c["metric"] == "missing"


def test_presentmon_capped_frames_not_cpu(monza120):
    b = monza120.analysis["presentmon"]["bound_pct"]
    assert b["cap"] > 90 and b["cpu"] < 10  # clavado en el tope: no es «limitado por CPU»
