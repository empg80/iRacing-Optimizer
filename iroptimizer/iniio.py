"""Lectura/escritura de los .ini de iRacing conservando comentarios, orden y finales de línea.

iRacing documenta cada clave con un comentario en línea (`Clave=valor ; descripción`),
así que nunca reescribimos el archivo entero: solo sustituimos el valor de la línea concreta.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .safety import safe_value

_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]")
_KV = re.compile(
    r"^(?P<lead>\s*)(?P<key>[^=;\[\s][^=;]*?)(?P<eq>\s*=\s*)(?P<val>[^;\r\n]*?)"
    r"(?P<pad>\s*)(?P<comment>;[^\r\n]*)?(?P<eol>\r?\n)?$"
)


class IniFile:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig")
            self.encoding = "utf-8"
        except UnicodeDecodeError:  # iRacing suele escribir ANSI
            text = raw.decode("cp1252", errors="replace")
            self.encoding = "cp1252"
        self.lines: list[str] = text.splitlines(keepends=True)
        self._index: list[tuple[str, str, int]] = []  # (section, key, line_no)
        self._reindex()

    def _reindex(self) -> None:
        self._index.clear()
        section = ""
        for i, line in enumerate(self.lines):
            m = _SECTION.match(line)
            if m:
                section = m.group("name").strip()
                continue
            if line.lstrip().startswith(";"):
                continue
            m = _KV.match(line)
            if m:
                self._index.append((section, m.group("key").strip(), i))

    def _find(self, key: str, section: str | None = None) -> int | None:
        k = key.lower()
        for sec, name, i in self._index:
            if name.lower() == k and (section is None or sec.lower() == section.lower()):
                return i
        return None

    def section_of(self, key: str) -> str | None:
        k = key.lower()
        for sec, name, _ in self._index:
            if name.lower() == k:
                return sec
        return None

    def get(self, key: str, section: str | None = None) -> str | None:
        i = self._find(key, section)
        if i is None:
            return None
        return _KV.match(self.lines[i]).group("val").strip()

    def get_num(self, key: str, section: str | None = None) -> float | None:
        v = self.get(key, section)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def set(self, key: str, value, section: str | None = None) -> bool:
        """Cambia el valor de una clave existente. Devuelve False si no existe."""
        i = self._find(key, section)
        if i is None:
            return False
        value = safe_value(_fmt(value))  # nunca escribir saltos de línea, ';', '=' ni corchetes
        m = _KV.match(self.lines[i])
        pad = m.group("pad") or ""
        comment = m.group("comment") or ""
        if comment and not pad:
            pad = " "
        self.lines[i] = (
            f"{m.group('lead')}{m.group('key')}{m.group('eq')}{value}"
            f"{pad}{comment}{m.group('eol') or ''}"
        )
        return True

    def as_dict(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for sec, key, i in self._index:
            out.setdefault(sec, {})[key] = _KV.match(self.lines[i]).group("val").strip()
        return out

    def flat(self) -> dict[str, str]:
        """{clave_en_minúsculas: valor}. Si una clave se repite, gana la primera."""
        out: dict[str, str] = {}
        for sec, key, i in self._index:
            out.setdefault(key.lower(), _KV.match(self.lines[i]).group("val").strip())
        return out

    def save(self, path: str | Path | None = None) -> None:
        target = Path(path) if path else self.path
        data = "".join(self.lines).encode(self.encoding, errors="replace")
        if self.bom:
            data = b"\xef\xbb\xbf" + data
        # escritura atómica: si algo falla a mitad, el .ini original queda intacto
        fd, tmp = tempfile.mkstemp(prefix=".iro_", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.6f}"
    return str(value)
