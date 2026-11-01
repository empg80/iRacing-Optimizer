@echo off
REM Compila el ejecutable en local (Windows). Resultado: dist\iRacingOptimizer.exe
REM Antes: descarga PresentMon-x.y.z-x64.exe de https://github.com/GameTechDev/PresentMon/releases y guárdalo como tools\PresentMon.exe
py -m pip install -r requirements.txt || exit /b 1
REM Bootloader compilado desde el código fuente (necesita Visual Studio Build Tools); reduce falsos positivos
set PYINSTALLER_COMPILE_BOOTLOADER=1
py -m pip install --no-cache-dir --no-binary pyinstaller pyinstaller==6.22.3 || exit /b 1
py packaging\make_version_info.py || exit /b 1
py -m PyInstaller --noconfirm --onefile --windowed --noupx --name iRacingOptimizer --icon assets\icon.ico --version-file build\version_info.txt ^
  --add-data "assets/icon.png;assets" --add-data "assets/icon.ico;assets" ^
  --add-data "tools/PresentMon.exe;tools" --add-data "THIRD_PARTY_NOTICES.md;." ^
  --hidden-import irsdk --hidden-import pynvml --hidden-import yaml app.py
