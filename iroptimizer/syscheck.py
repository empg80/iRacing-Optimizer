"""Chequeo del sistema antes de correr: BIOS, RAM, gráfica, pantallas y Windows.

Usa los datos de sysinfo.collect() guardados en hardware["system"] (también en cada sesión grabada), así que
sirve igual para el equipo propio que para una sesión exportada por otro usuario.

Lo que no se puede leer sin drivers especiales ni la API privada de NVIDIA (G-SYNC en ventana, ajustes del
perfil del driver, voltaje SoC, HDR) va como lista de comprobación manual con instrucciones.
"""
from __future__ import annotations

import re
from datetime import date

from .diagnostics import BAD, INFO, OK, STATUS_ORDER, WARN, Check
from .i18n import _

DEDICATED = re.compile(r"nvidia|geforce|\brtx\b|\bgtx\b|radeon\s*(rx|pro)|\barc\s*[ab]\d", re.I)
AM5_X3D = re.compile(r"\b[79]\d{3}X3D\b", re.I)
SOC_FIX = date(2023, 5, 1)  # BIOS con el límite de 1,30 V de SoC para X3D (AGESA 1.0.0.7)


def _area():
    return _("Equipo")


def _bios_date(s: str | None) -> date | None:
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def is_dedicated(adapter: str | None) -> bool:
    return bool(adapter and DEDICATED.search(adapter))


def evaluate(hw: dict) -> list[dict]:
    """Comprobaciones automáticas a partir de `hardware`. Lista vacía si no hay datos del sistema."""
    sysd = (hw or {}).get("system") or {}
    if not sysd:
        return []
    checks: list[dict] = []

    def add(title, status, metric, detail, actions=None):
        checks.append(Check(_area(), title, status, metric, detail, actions).d)

    cpu = ((hw.get("cpu") or {}).get("name")) or ""
    gpus = hw.get("gpus") or []

    # ------------------------------------------------------------------ RAM
    mods = [m for m in sysd.get("memory") or [] if m.get("configured")]
    if mods:
        typ = next((m["type"] for m in mods if m.get("type")), None) or "RAM"
        conf = min(m["configured"] for m in mods)
        spd = max((m.get("speed") or 0) for m in mods)
        metric = _("{typ} a {conf} MT/s · {n} módulos", typ=typ, conf=conf, n=len(mods))
        base = (typ == "DDR5" and conf <= 4800) or (typ == "DDR4" and conf <= 2400)
        if base:
            add(_("Velocidad de la RAM"), WARN, metric,
                _("La RAM funciona a la velocidad base (JEDEC): parece que el perfil EXPO/XMP está desactivado. iRacing suele ir limitado por la CPU y la memoria lenta se nota en los FPS mínimos."),
                [_("Activa EXPO (AMD) o XMP (Intel) en la BIOS."),
                 _("Después comprueba en el Administrador de tareas → Rendimiento → Memoria que la velocidad es la de tu kit (por ejemplo 6000 MT/s).")])
        elif spd and conf < spd - 100:
            add(_("Velocidad de la RAM"), INFO, metric + _(" · el módulo declara {spd}", spd=spd),
                _("La RAM va por debajo de la velocidad que declaran los módulos. Puede ser a propósito (estabilidad, 4 módulos), pero revisa el perfil EXPO/XMP."),
                [_("En la BIOS, comprueba que el perfil EXPO/XMP está activado.")])
        else:
            add(_("Velocidad de la RAM"), OK, metric, _("La RAM funciona por encima de la velocidad base: el perfil EXPO/XMP está activo."))

    # ------------------------------------------------------------------ BIOS
    bios = sysd.get("bios") or {}
    board = sysd.get("board") or {}
    bdate = _bios_date(bios.get("date"))
    if bios.get("version") or bdate:
        metric = " · ".join(x for x in (board.get("product"), _("BIOS {v}", v=bios.get("version") or "?"),
                                        bios.get("date")) if x)
        if bdate and AM5_X3D.search(cpu) and bdate < SOC_FIX:
            add(_("BIOS"), BAD, metric,
                _("BIOS anterior a mayo de 2023 con un Ryzen X3D: estas versiones podían subir el voltaje SoC por encima de 1,30 V con EXPO activado, lo que ha llegado a dañar procesadores."),
                [_("Actualiza la BIOS a la última versión de la web del fabricante de tu placa."),
                 _("Tras actualizar, vuelve a activar EXPO (la actualización borra los ajustes de la BIOS).")])
        else:
            add(_("BIOS"), OK, metric, _("Versión y fecha de la BIOS. Actualizarla solo hace falta si hay un motivo (seguridad, estabilidad, CPU nueva)."))

    # ------------------------------------------------------------------ gráfica: ReBAR y PCIe
    links = sysd.get("gpu_links") or []
    if links:
        ln = links[0]
        gname = (gpus[0].get("name") if gpus else "") or ""
        bar1 = ln.get("bar1_mb")
        if bar1:
            if bar1 <= 256:
                supported = bool(re.search(r"RTX\s*(30|40|50)\d\d", gname, re.I))
                add(_("Resizable BAR"), WARN if supported else INFO, _("Desactivado (BAR1 {mb} MB)", mb=bar1),
                    _("Con Resizable BAR la CPU accede a toda la memoria de la gráfica de golpe. Tu gráfica lo admite y está desactivado.")
                    if supported else _("Resizable BAR está desactivado; tu gráfica puede no admitirlo."),
                    [_("En la BIOS: activa «Above 4G Decoding» y «Re-Size BAR Support» (CSM debe estar desactivado)."),
                     _("Compruébalo en el Panel de control de NVIDIA → Información del sistema → Resizable BAR.")] if supported else [])
            else:
                add(_("Resizable BAR"), OK, _("Activado ({gb:.0f} GB)", gb=bar1 / 1024), _("La CPU puede acceder a toda la memoria de la gráfica."))
        w, wm, gmax, ggpu = ln.get("width"), ln.get("width_max"), ln.get("gen_max"), ln.get("gen_gpu")
        if w and wm:
            metric = f"x{w}/x{wm}" + (f" · PCIe {gmax}.0" if gmax else "")
            if w < wm:
                add(_("Enlace PCIe de la gráfica"), WARN, metric,
                    _("La gráfica está funcionando con menos líneas PCIe de las que admite. Suele ser una ranura secundaria, un SSD M.2 que comparte líneas con la gráfica o la tarjeta mal insertada."),
                    [_("Pon la gráfica en la ranura PCIe superior (la más cercana a la CPU)."),
                     _("Revisa en el manual de la placa qué ranuras M.2 comparten líneas con la gráfica."),
                     _("Compruébalo con GPU-Z o CPU-Z (pestaña Mainboard → Graphic Interface).")])
            elif gmax and ggpu and gmax < ggpu and gmax <= 3:
                add(_("Enlace PCIe de la gráfica"), INFO, metric + _(" · la gráfica admite PCIe {g}.0", g=ggpu),
                    _("La placa o la ranura limita la generación PCIe. Con x16 la pérdida es pequeña; a x8 en PCIe 3.0 ya se puede notar."))
            else:
                add(_("Enlace PCIe de la gráfica"), OK, metric, _("La gráfica usa todas sus líneas PCIe."))

    # ------------------------------------------------------------------ pantallas
    disps = sysd.get("displays") or []
    prim = next((d for d in disps if d.get("primary")), disps[0] if disps else None)
    any_dedicated = any(is_dedicated(d.get("adapter")) for d in disps) or any(
        is_dedicated(g.get("name")) for g in gpus)
    if prim:
        r, rm = prim.get("refresh") or 0, prim.get("refresh_max") or 0
        metric = f"{prim.get('width')}×{prim.get('height')} @ {r} Hz"
        if rm >= 100 and rm - r >= 10:
            add(_("Frecuencia de la pantalla principal"), WARN, metric + _(" · admite {hz} Hz", hz=rm),
                _("Windows tiene la pantalla a menos frecuencia de la que admite. Es muy habitual después de instalar un driver o cambiar de cable."),
                [_("Configuración → Sistema → Pantalla → Pantalla avanzada → Elegir una frecuencia de actualización: pon la máxima."),
                 _("Si no aparece, usa un cable DisplayPort (o HDMI 2.1) conectado a la tarjeta gráfica.")])
        else:
            add(_("Frecuencia de la pantalla principal"), OK, metric, _("La pantalla principal está a su frecuencia máxima."))
        if any_dedicated and not is_dedicated(prim.get("adapter")):
            add(_("Pantalla principal en la gráfica integrada"), BAD, prim.get("adapter") or "?",
                _("El monitor principal está conectado a la placa base. iRacing se dibuja en la tarjeta gráfica y cada imagen se copia a la integrada, o directamente se dibuja con la integrada: pierdes rendimiento y añades latencia."),
                [_("Conecta el monitor principal a una salida de la tarjeta gráfica, no a la de la placa base.")])
    for d in disps:
        if d is prim or is_dedicated(d.get("adapter")) or not any_dedicated:
            continue
        add(_("Pantalla secundaria en la gráfica integrada"), INFO, f"{d.get('name', '')} · {d.get('adapter') or '?'}",
            _("Las ventanas de esa pantalla (SimHub, overlays) se suelen dibujar en la tarjeta gráfica y se copian a la integrada a través de la RAM: no alivia a la CPU y añade algo de trabajo. Si lo hiciste para ganar rendimiento, compara dos sesiones antes de dejarlo así."),
            [_("Conéctala a la tarjeta gráfica si tiene salidas libres."),
             _("Si la dejas en la integrada, pon esos programas en «Ahorro de energía» en Configuración → Pantalla → Gráficos.")])

    # ------------------------------------------------------------------ Windows
    win = sysd.get("windows") or {}
    if (win.get("build") or 0) >= 22000:
        wo = win.get("windowed_opt")
        if wo is True:
            add(_("Optimizaciones para juegos en ventana"), OK, _("Activadas"),
                _("Los juegos en ventana usan el mismo modo de presentación que en pantalla completa (menos latencia, VRR)."))
        else:
            add(_("Optimizaciones para juegos en ventana"), WARN if wo is False else INFO,
                _("Desactivadas") if wo is False else _("Sin configurar (valor de Windows)"),
                _("iRacing en ventana o ventana sin bordes pasa por el compositor de Windows. Con esta opción usa el modo de presentación moderno: menos latencia y G-SYNC/FreeSync más fiable."),
                [_("Configuración → Sistema → Pantalla → Gráficos → Cambiar la configuración de gráficos predeterminada → «Optimizaciones para juegos en ventana»: activado.")])
    bg = win.get("background_recording")
    if bg is True:
        add(_("Grabación en segundo plano de Xbox Game Bar"), WARN, _("Activada"),
            _("Windows graba continuamente los últimos minutos de juego. Usa el codificador de la gráfica y algo de CPU y disco todo el rato."),
            [_("Configuración → Juegos → Capturas → «Grabar lo que ha pasado»: desactivado.")])
    elif bg is False:
        add(_("Grabación en segundo plano de Xbox Game Bar"), OK, _("Desactivada"), _("Windows no está grabando en segundo plano."))

    checks.sort(key=lambda c: STATUS_ORDER[c["status"]])
    return checks


def manual_items(hw: dict) -> list[dict]:
    """Comprobaciones que la app no puede leer: título e instrucciones, según la gráfica y la pantalla."""
    gpus = (hw or {}).get("gpus") or []
    gname = " ".join(g.get("name") or "" for g in gpus)
    nvidia = any(g.get("vendor") == "nvidia" for g in gpus) or "nvidia" in gname.lower() or "geforce" in gname.lower()
    amd = bool(re.search(r"radeon\s*(rx|pro)", gname, re.I))
    refresh = ((hw or {}).get("display") or {}).get("refresh") or 0
    cpu = ((hw or {}).get("cpu") or {}).get("name") or ""
    items = []
    if refresh > 60:
        items.append({"id": "vrr_monitor", "title": _("Adaptive-Sync / FreeSync activado en el monitor"),
                      "how": _("En el menú del monitor (OSD), busca Adaptive-Sync, FreeSync o VRR y actívalo. En algunos monitores solo funciona con el modo juego activado.")})
        if nvidia:
            items.append({"id": "gsync_windowed", "title": _("G-SYNC activado en pantalla completa y en ventana"),
                          "how": _("Panel de control de NVIDIA (Microsoft Store) → Pantalla → Configurar G-SYNC → «Habilitar para el modo de ventana y de pantalla completa». Para comprobarlo: menú Pantalla → Indicador de G-SYNC.")})
        elif amd:
            items.append({"id": "freesync_driver", "title": _("FreeSync activado en AMD Adrenalin"),
                          "how": _("AMD Software → Pantalla → AMD FreeSync: activado.")})
    if nvidia:
        items += [
            {"id": "nv_profile", "title": _("Perfil de iRacing en NVIDIA App"),
             "how": _("Gráficos → Configuración de programa → iRacing (si no aparece, «Añadir» → iRacingSim64DX11.exe): Sincronización vertical activada (y desactivada en iRacing), Modo de baja latencia desactivado si usas Reflex, Velocidad máx. de fotogramas desactivada, Gestión de energía «Preferir rendimiento máximo».")},
            {"id": "nv_shader_cache", "title": _("Caché de sombreadores de NVIDIA"),
             "how": _("Gráficos → Configuración global → Tamaño de caché de sombreadores: predeterminado o 10 GB. Revísalo tras actualizar el driver.")},
            {"id": "nv_replay", "title": _("Grabación instantánea de NVIDIA desactivada si emites con OBS"),
             "how": _("NVIDIA App → superposición (Alt+Z) → Grabación instantánea: desactivada. Si ya grabas con OBS, las dos compiten por el codificador.")},
        ]
    items.append({"id": "hdr", "title": _("HDR desactivado salvo monitor OLED o mini-LED"),
                  "how": _("Configuración → Pantalla → HDR. Los monitores con retroiluminación por bordes no dan HDR real, y con HDR de Windows iRacing puede perder overlays.")})
    if AM5_X3D.search(cpu):
        items.append({"id": "soc_voltage", "title": _("Voltaje SoC por debajo de 1,30 V (Ryzen X3D)"),
                      "how": _("Con OCCT o HWiNFO, mira «CPU VDDCR_SOC». Con EXPO lo normal es 1,20–1,25 V. Si pasa de 1,30 V, actualiza la BIOS.")})
    return items
