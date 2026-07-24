@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ======================================
echo   Cancellation Rate Analyzer
echo ======================================
echo.

echo [1/3] Checking Python...
python --version
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    pause
    exit /b
)

echo.
echo [2/3] Installing packages (flask, pandas)...
python -m pip install flask pandas
if errorlevel 1 (
    echo [ERROR] pip install failed. See message above.
    pause
    exit /b
)

echo.
echo [3/3] Starting server...
echo.
echo   Server: http://127.0.0.1:5150
echo   (browser opens in 4 seconds)
echo   Press Ctrl+C here to stop
echo.

start "" cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:5150"

python app.py

echo.
echo [Server stopped]
pause
