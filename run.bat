@echo off
setlocal enabledelayedexpansion

set BACKEND_PORT=8000
set FRONTEND_PORT=5173

echo [run.bat] Freeing port %BACKEND_PORT% and %FRONTEND_PORT%...

for %%P in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr /R /C:":%%P .*LISTENING"') do (
        echo   killing PID %%A on port %%P
        taskkill /F /PID %%A >nul 2>&1
    )
)

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [run.bat] ERROR: .venv not found. Run: uv sync
    pause
    exit /b 1
)

if not exist "frontend\node_modules\vite\bin\vite.js" (
    echo [run.bat] ERROR: frontend deps missing. Run: cd frontend ^&^& npm install
    pause
    exit /b 1
)

echo [run.bat] Starting backend on :%BACKEND_PORT%...
start "invoice-ai backend" cmd /k ""%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port %BACKEND_PORT%"

echo [run.bat] Starting frontend on :%FRONTEND_PORT%...
start "invoice-ai frontend" cmd /k "cd /d "%~dp0frontend" && node node_modules\vite\bin\vite.js --host 127.0.0.1 --port %FRONTEND_PORT%"

echo.
echo [run.bat] Backend:  http://127.0.0.1:%BACKEND_PORT%
echo [run.bat] Frontend: http://127.0.0.1:%FRONTEND_PORT%
echo.
echo Close the two spawned windows to stop the servers.
endlocal
