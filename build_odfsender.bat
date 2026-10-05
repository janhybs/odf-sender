@echo off
setlocal
cd /d "%~dp0"

python -m PyInstaller --clean --noconfirm --onefile --console --name OdfSender --exclude-module numpy --exclude-module PIL --exclude-module pygments odfsender.py
if errorlevel 1 goto build_failed

echo.
echo Build complete: dist\OdfSender.exe
pause
exit /b 0

:build_failed
echo.
echo PyInstaller build failed.
pause
exit /b 1
