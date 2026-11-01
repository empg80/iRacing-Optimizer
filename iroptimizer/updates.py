"""Aviso de nueva versión (opcional y desactivado por defecto).

Es la única conexión a internet de la app y solo ocurre si el usuario lo activa:
- consulta al abrir la app la API pública de GitHub (`releases/latest` del repositorio oficial);
- no envía nada del usuario: es una petición GET anónima, sin datos de equipo ni de sesiones;
- nunca descarga ni ejecuta nada: solo avisa y abre la página oficial de la release en el navegador.
Se puede desactivar del todo con la variable de entorno IROPT_NO_UPDATE_CHECK=1.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

REPO = "empg80/iRacing-Optimizer"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
_PAGE_PREFIX = f"https://github.com/{REPO}/releases/".lower()
_DL_PREFIX = f"https://github.com/{REPO}/releases/download/".lower()
EXE_NAME = "iRacingOptimizer.exe"
CHECK_EVERY_S = 0  # cada arranque (la API anónima de GitHub admite 60 consultas/hora por IP)
MAX_BYTES = 1_000_000
_VER_RE = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")


def parse_version(tag) -> tuple[int, int, int] | None:
    """'v0.9.0' o '0.9.0' → (0, 9, 0). Cualquier otra cosa → None."""
    m = _VER_RE.match(str(tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def disabled_by_env() -> bool:
    return os.environ.get("IROPT_NO_UPDATE_CHECK", "").strip() not in ("", "0")


def should_check(settings: dict, now: float | None = None) -> bool:
    """Solo si el usuario lo activó y no está desactivado por entorno (CHECK_EVERY_S permite espaciar las consultas)."""
    if disabled_by_env() or settings.get("update_check") is not True:
        return False
    now = time.time() if now is None else now
    try:
        last = float(settings.get("update_last_check") or 0)
    except (TypeError, ValueError):
        last = 0
    return now - last >= CHECK_EVERY_S


def _safe_url(url, prefix: str, fallback: str) -> str:
    """Solo se abren enlaces del repositorio oficial en github.com."""
    u = str(url or "")
    return u if u.lower().startswith(prefix) and re.fullmatch(r"[A-Za-z0-9:/._\-%]+", u) else fallback


def parse_release(data: dict) -> dict | None:
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    ver = parse_version(data.get("tag_name"))
    if not ver:
        return None
    page = _safe_url(data.get("html_url"), _PAGE_PREFIX, RELEASES_URL)
    dl = page
    for a in data.get("assets") or []:
        if isinstance(a, dict) and a.get("name") == EXE_NAME:
            dl = _safe_url(a.get("browser_download_url"), _DL_PREFIX, page)
    return {"version": ".".join(map(str, ver)), "tuple": ver, "page_url": page, "download_url": dl}


def fetch_latest(timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "iRacingOptimizer-update-check"})
    # URL fija https a api.github.com: no depende de datos del usuario
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        return None
    return parse_release(json.loads(body.decode("utf-8")))


def newer_release(current: str, settings: dict | None = None, timeout: float = 5.0) -> dict | None:
    """La release más reciente si es posterior a `current` y no la has ignorado. None si no hay o falla la red."""
    cur = parse_version(current)
    try:
        rel = fetch_latest(timeout)
    except Exception:
        return None  # sin internet, GitHub caído, respuesta rara: la app sigue sin más
    if not rel or not cur or rel["tuple"] <= cur:
        return None
    if (settings or {}).get("update_skip_version") == rel["version"]:
        return None
    return rel
