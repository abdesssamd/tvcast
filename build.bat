@echo off
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --name MultiTV --add-data "static;static" --collect-all imageio_ffmpeg --collect-all pychromecast --collect-all zeroconf --hidden-import dlna --hidden-import capture --hidden-import castdev --hidden-import diag server.py
echo.
echo Termine : dist\MultiTV.exe
pause
