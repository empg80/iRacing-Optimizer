"""Cálculo geométrico del FOV para iRacing (plano, curvo, triples planos y triples curvos).

Medidas como las pide iRacing en Ajustes → Pantalla → Monitor:
- Ancho del monitor: de borde a borde INCLUIDOS biseles, en línea recta (en curvos, sin seguir la curva).
- Bisel: el de UN lado.
- Ángulo entre central y laterales: solo con "Render scene using 3 projections".
- Radio de curvatura (p. ej. 1000R = 1000 mm) en lugar del ángulo para monitores curvos.
- Distancia de visión: de los ojos al centro del monitor central.

El FOV de iRacing (drivingCamFOV en app.ini) es horizontal. Aquí se calcula el ángulo que el área
visible ocupa realmente desde tus ojos (FOV 1:1). El botón "Compute" de iRacing hace el cálculo de
referencia; esta herramienta lo replica para poder visualizarlo y guardarlo, pero ante una
diferencia de pocos grados manda el de iRacing.

Claves en rendererDX11*.ini → [MonitorSetup]:
  MonitorType (0 = plano, 1 = curvo), NumMonitors (1 o 3), MonitorWidth (mm con biseles),
  ScreenWidth (mm visibles), RadiusOfCurvature (mm), ScreenAngles (°), ViewingDist (mm),
  RenderViewPerMonitor (1 = 3 proyecciones), BezelProtectionPct.
"""
from __future__ import annotations

from .i18n import _

import math
from dataclasses import asdict, dataclass

ASPECTS = {"16:9": (16, 9), "16:10": (16, 10), "21:9": (64, 27), "32:9": (32, 9), "4:3": (4, 3)}


def width_from_diagonal(diag_in: float, aspect: str = "16:9", curved_radius: float | None = None) -> float:
    """Ancho visible (mm) a partir de la diagonal. En curvos devuelve la cuerda (medida recta)."""
    w, h = ASPECTS.get(aspect, (16, 9))
    arc = diag_in * 25.4 * w / math.hypot(w, h)
    if curved_radius:
        return 2 * curved_radius * math.sin(arc / (2 * curved_radius))
    return arc


@dataclass
class Setup:
    monitors: int = 1            # 1 o 3
    curved: bool = False
    monitor_width: float = 600   # mm, borde a borde con biseles (recto)
    bezel: float = 10            # mm, un lado
    distance: float = 600        # mm, ojos → centro del monitor central
    side_angle: float = 45       # °, giro de los laterales (planos)
    radius: float = 1000         # mm (curvos)
    screen_height: float | None = None  # mm visibles, para el FOV vertical
    three_projections: bool = True

    @property
    def screen_width(self) -> float:
        return max(1.0, self.monitor_width - 2 * self.bezel)


@dataclass
class Result:
    fov_h: float                 # FOV horizontal total que ocupan las pantallas (grados)
    fov_center: float            # FOV del monitor central
    fov_v: float | None
    ideal_side_angle: float | None
    outline: list[list[tuple[float, float]]]   # polilíneas de las pantallas (x, z) con el ojo en (0, 0)
    edges: tuple[tuple[float, float], tuple[float, float]]  # bordes visibles extremos
    warnings: list[str]

    def as_dict(self):
        return asdict(self)


def _angle(p):
    x, z = p
    return math.degrees(math.atan2(abs(x), z))


def _arc_points(radius, center_z, a0, a1, n=40):
    """Puntos de un arco centrado en (0, center_z) entre los ángulos a0..a1 (rad, 0 = frente)."""
    return [(radius * math.sin(a0 + (a1 - a0) * i / n), center_z - radius * math.cos(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


def compute(s: Setup) -> Result:
    warns: list[str] = []
    d, sw, b = s.distance, s.screen_width, s.bezel
    vfov = math.degrees(2 * math.atan((s.screen_height / 2) / d)) if s.screen_height else None

    if s.curved:
        R = s.radius
        if R <= 0:
            raise ValueError(_("Radio de curvatura no válido"))
        # Centro de la pantalla central en z = d; centro de curvatura en z = d - R (detrás o delante del ojo).
        cz = d - R
        half_vis = math.asin(min(1.0, (sw / 2) / R))          # semiángulo visible (desde el centro de curvatura)
        half_mon = math.asin(min(1.0, (s.monitor_width / 2) / R))
        if s.monitors == 1:
            angs = [(-half_vis, half_vis)]
        else:
            # Tres paneles curvos sobre la misma circunferencia (iRacing pide radio en vez de ángulo)
            step = 2 * half_mon
            angs = [(k * step - half_vis, k * step + half_vis) for k in (-1, 0, 1)]
            if step + half_vis >= math.pi / 2:
                warns.append(_("Con este radio y ancho, los laterales quedan casi de lado: revisa el radio."))
        outline = [[(R * math.sin(a0 + (a1 - a0) * i / 40), cz + R * math.cos(a0 + (a1 - a0) * i / 40))
                    for i in range(41)] for a0, a1 in angs]
        left, right = outline[0][0], outline[-1][-1]
        cen = outline[len(outline) // 2]
        fov_center = _angle(cen[0]) + _angle(cen[-1])
        fov_h = 2 * math.degrees(math.atan2(abs(right[0]), right[1]))
        ideal = None
        if d < R * 0.3:
            warns.append(_("Estás muy cerca para este radio: revisa la distancia."))
    else:
        # Planos. Central centrado en z = d.
        c_left, c_right = (-sw / 2, d), (sw / 2, d)
        outline = [[c_left, c_right]]
        fov_center = 2 * math.degrees(math.atan((sw / 2) / d))
        ideal = None
        if s.monitors == 3:
            a = math.radians(s.side_angle)
            hinge = sw / 2 + b                       # borde físico del monitor central
            # lateral derecho: empieza tras su bisel y gira hacia el usuario
            def side(sign, a):
                p0 = (sign * (hinge + b * math.cos(a)), d - b * math.sin(a))
                p1 = (sign * (hinge + (b + sw) * math.cos(a)), d - (b + sw) * math.sin(a))
                return p0, p1
            l0, l1 = side(-1, a)
            r0, r1 = side(1, a)
            outline = [[l1, l0], [c_left, c_right], [r0, r1]]
            x, z = r1
            fov_h = 2 * math.degrees(math.atan2(abs(x), z))
            # ángulo ideal: el centro del lateral mirando a los ojos (perpendicular a la línea de visión)
            lo, hi = 0.0, math.radians(89)
            for _it in range(60):
                mid = (lo + hi) / 2
                p0, p1 = side(1, mid)
                cxs, czs = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
                tx, tz = p1[0] - p0[0], p1[1] - p0[1]
                dot = cxs * tx + czs * tz   # >0: el panel aún "mira" hacia fuera → falta girar
                if dot > 0:
                    lo = mid
                else:
                    hi = mid
            ideal = math.degrees((lo + hi) / 2)
            if not s.three_projections:
                warns.append(_("Sin «3 proyecciones» iRacing ignora el ángulo de los laterales y la imagen se deforma en ellos."))
            if abs(s.side_angle - ideal) > 8:
                warns.append(_("El ángulo de los laterales ({p0:.0f}°) está lejos del ideal ({ideal:.0f}°).", p0=s.side_angle, ideal=ideal))
        else:
            fov_h = fov_center
        left, right = outline[0][0], outline[-1][-1]

    if s.monitors == 1 and not s.curved and fov_h > 100:
        warns.append(_("FOV muy alto para un monitor plano: probablemente la distancia es demasiado corta o el ancho está mal."))
    return Result(round(fov_h, 1), round(fov_center, 1), None if vfov is None else round(vfov, 1),
                  None if ideal is None else round(ideal, 1), outline, (left, right), warns)


def setup_from_ini(renderer_flat: dict, app_flat: dict | None = None) -> tuple[Setup, float | None]:
    """Lee [MonitorSetup] del renderer y drivingCamFOV de app.ini."""
    def f(k, default):
        try:
            return float(renderer_flat.get(k.lower(), default))
        except (TypeError, ValueError):
            return default
    mw = f("MonitorWidth", 0) or 600
    sw = f("ScreenWidth", 0) or mw - 20
    s = Setup(monitors=3 if f("NumMonitors", 1) >= 3 else 1, curved=f("MonitorType", 0) == 1,
              monitor_width=mw, bezel=max(0.0, (mw - sw) / 2), distance=f("ViewingDist", 0) or 600,
              side_angle=f("ScreenAngles", 0) or 45, radius=f("RadiusOfCurvature", 1000) or 1000,
              three_projections=f("RenderViewPerMonitor", 0) == 1)
    cur = None
    if app_flat:
        try:
            cur = float(app_flat.get("drivingcamfov"))
        except (TypeError, ValueError):
            cur = None
    return s, cur


def ini_changes(s: Setup, fov_value: float) -> list[dict]:
    """Cambios a escribir (solo se aplican si las claves existen)."""
    r = [("MonitorType", 1 if s.curved else 0), ("NumMonitors", s.monitors), ("MonitorWidth", round(s.monitor_width)),
         ("ScreenWidth", round(s.screen_width)), ("ViewingDist", round(s.distance)),
         ("RenderViewPerMonitor", 1 if s.three_projections else 0)]
    if s.curved:
        r.append(("RadiusOfCurvature", round(s.radius)))
    elif s.monitors == 3:
        r.append(("ScreenAngles", round(s.side_angle)))
    out = [{"file": "renderer", "section": "MonitorSetup", "key": k, "proposed": str(v)} for k, v in r]
    out.append({"file": "app", "section": "View", "key": "drivingCamFOV", "proposed": f"{fov_value:.6f}"})
    return out
