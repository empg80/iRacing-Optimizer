@echo off
REM Compila el ejecutable en local (Windows). Resultado: dist\iRacingOptimizer.exe
py -m pip install -r requirements.txt pyinstaller || exit /b 1
py -m PyInstaller --noconfirm --onefile --windowed --name iRacingOptimizer --icon assets\icon.ico ^
  --add-data "assets/icon.png;assets" --add-data "assets/icon.ico;assets" ^
  --hidden-import irsdk --hidden-import pynvml --hidden-import yaml app.py
