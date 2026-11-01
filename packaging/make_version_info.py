"""Genera build/version_info.txt (recurso VERSIONINFO del .exe) a partir de iroptimizer.__version__.

Los antivirus heurísticos puntúan peor un .exe sin nombre de producto, autor ni versión;
estos datos también aparecen en Propiedades → Detalles del archivo en Windows.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from iroptimizer import __version__  # noqa: E402

m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", __version__)
if not m:
    raise SystemExit(f"Versión no válida: {__version__}")
nums = tuple(int(x) for x in m.groups()) + (0,)

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'empg80 (open source project)'),
      StringStruct('FileDescription', 'iRacing Optimizer - graphics and performance analysis for iRacing'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'iRacingOptimizer'),
      StringStruct('LegalCopyright', 'MIT License - https://github.com/empg80/iRacing-Optimizer'),
      StringStruct('OriginalFilename', 'iRacingOptimizer.exe'),
      StringStruct('ProductName', 'iRacing Optimizer'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

out = ROOT / "build" / "version_info.txt"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(TEMPLATE.format(nums=nums, ver=__version__), encoding="utf-8")
print(out)
