"""v0.10: chequeo del sistema (BIOS, RAM, gráfica, pantallas, Windows)."""
from __future__ import annotations

import struct

from iroptimizer import syscheck
from iroptimizer.sysinfo import parse_smbios


def _struct(typ, fields: bytes, strings: list[str]) -> bytes:
    body = bytes([typ, 4 + len(fields)]) + b"\x00\x00" + fields
    tail = b"".join(s.encode() + b"\x00" for s in strings) + b"\x00" if strings else b"\x00\x00"
    return body + tail


def _mem(conf, speed=4800, typ=0x22, size=16384):
    f = bytearray(0x28 - 4)  # campos a partir del offset 4
    def put(off, fmt, v):
        struct.pack_into(fmt, f, off - 4, v)
    put(0x0C, "<H", size)
    f[0x12 - 4] = typ
    put(0x15, "<H", speed)
    f[0x1A - 4] = 1  # part number = cadena 1
    put(0x20, "<H", conf)
    return _struct(17, bytes(f), ["KF560C30-16"])


def _table(conf=6000, bios_date="11/13/2025"):
    bios = bytearray(0x12 - 4)
    bios[0] = 1; bios[1] = 2; bios[4] = 3  # vendor, version, date
    board = bytearray(0x08 - 4)
    board[0] = 1; board[1] = 2
    return (_struct(0, bytes(bios), ["American Megatrends Inc.", "3602", bios_date])
            + _struct(2, bytes(board), ["ASUSTeK COMPUTER INC.", "TUF GAMING B650M-PLUS WIFI"])
            + _mem(conf) + _mem(conf) + _struct(127, b"", []))


def test_parse_smbios():
    d = parse_smbios(_table())
    assert d["bios"] == {"vendor": "American Megatrends Inc.", "version": "3602", "date": "11/13/2025"}
    assert d["board"]["product"] == "TUF GAMING B650M-PLUS WIFI"
    assert len(d["memory"]) == 2
    m = d["memory"][0]
    assert m == {"size_mb": 16384, "type": "DDR5", "speed": 4800, "configured": 6000, "part": "KF560C30-16"}


def _hw(**sysd):
    return {"cpu": {"name": "AMD Ryzen 7 7800X3D 8-Core Processor"},
            "gpus": [{"name": "NVIDIA GeForce RTX 4090", "vendor": "nvidia"}],
            "display": {"refresh": 240}, "system": sysd}


def _by_title(checks):
    return {c["title"]: c for c in checks}


def test_kike_rig_all_ok():
    """El equipo de Kike tras la revisión de nov-2026: todo correcto."""
    s = parse_smbios(_table())
    s.update(gpu_links=[{"bar1_mb": 32768, "width": 16, "width_max": 16, "gen_max": 4, "gen_gpu": 4}],
             displays=[{"name": "\\\\.\\DISPLAY1", "adapter": "NVIDIA GeForce RTX 4090", "primary": True,
                        "width": 5120, "height": 1440, "refresh": 240, "refresh_max": 240},
                       {"name": "\\\\.\\DISPLAY2", "adapter": "NVIDIA GeForce RTX 4090", "primary": False,
                        "width": 1280, "height": 800, "refresh": 60, "refresh_max": 60}],
             windows={"build": 26100, "windowed_opt": True, "background_recording": False})
    checks = syscheck.evaluate(_hw(**s))
    assert checks and all(c["status"] == "ok" for c in checks), [(c["title"], c["status"]) for c in checks]


def test_common_mistakes_detected():
    s = parse_smbios(_table(conf=4800, bios_date="02/10/2023"))
    s.update(gpu_links=[{"bar1_mb": 256, "width": 8, "width_max": 16, "gen_max": 4, "gen_gpu": 4}],
             displays=[{"name": "\\\\.\\DISPLAY1", "adapter": "AMD Radeon(TM) Graphics", "primary": True,
                        "width": 5120, "height": 1440, "refresh": 60, "refresh_max": 240}],
             windows={"build": 26100, "windowed_opt": False, "background_recording": True})
    t = _by_title(syscheck.evaluate(_hw(**s)))
    assert t["Velocidad de la RAM"]["status"] == "aviso"
    assert t["BIOS"]["status"] == "problema"  # X3D + BIOS antes del límite de SoC
    assert t["Resizable BAR"]["status"] == "aviso"
    assert t["Enlace PCIe de la gráfica"]["status"] == "aviso"
    assert t["Frecuencia de la pantalla principal"]["status"] == "aviso"
    assert t["Pantalla principal en la gráfica integrada"]["status"] == "problema"
    assert t["Optimizaciones para juegos en ventana"]["status"] == "aviso"
    assert t["Grabación en segundo plano de Xbox Game Bar"]["status"] == "aviso"
    assert list(t.values())[0]["status"] == "problema"  # ordenado por gravedad


def test_secondary_on_igpu_is_info():
    s = {"displays": [{"name": "D1", "adapter": "NVIDIA GeForce RTX 4090", "primary": True, "width": 2560,
                       "height": 1440, "refresh": 165, "refresh_max": 165},
                      {"name": "D2", "adapter": "AMD Radeon(TM) Graphics", "primary": False, "width": 800,
                       "height": 480, "refresh": 60, "refresh_max": 60}]}
    t = _by_title(syscheck.evaluate(_hw(**s)))
    assert t["Pantalla secundaria en la gráfica integrada"]["status"] == "info"


def test_no_data_no_checks_and_manual_items():
    assert syscheck.evaluate({}) == []
    ids = {i["id"] for i in syscheck.manual_items(_hw())}
    assert {"gsync_windowed", "nv_profile", "soc_voltage", "hdr"} <= ids


def test_old_sessions_without_system_data(tmp_path):
    """Las sesiones grabadas antes de v0.10 no traen hardware["system"]: el diagnóstico no cambia."""
    from iroptimizer import demo, pipeline
    csv_path, _ = demo.make(tmp_path)
    res = pipeline.run(csv_path)
    assert not any(c["area"] == "Equipo" for c in res.diagnostics["checks"])
