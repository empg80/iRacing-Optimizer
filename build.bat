@echo off
REM Compila el ejecutable en local (Windows). Resultado: dist\iRacingOptimizer.exe
REM Antes: descarga PresentMon-x.y.z-x64.exe de https://github.com/GameTechDev/PresentMon/releases y guárdalo como tools\PresentMon.exe
py -m pip install -r requirements.txt pyinstaller==6.22.3 || exit /b 1
py -m PyInstaller --noconfirm --onefile --windowed --name iRacingOptimizer --icon assets\icon.ico ^
  --add-data "assets/icon.png;assets" --add-data "assets/icon.ico;assets" ^
  --add-data "tools/PresentMon.exe;tools" --add-data "THIRD_PARTY_NOTICES.md;." ^
  --hidden-import irsdk --hidden-import pynvml --hidden-import yaml app.py
