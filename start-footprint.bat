@echo off
chcp 65001 >nul
title Footprint Launcher
cd /d "%~dp0"

echo.
echo === Footprint launcher ===
echo.

echo [1/3] Checking Docker Desktop...
docker info >nul 2>&1
if errorlevel 1 (
  echo   [X] Docker is not running.
  echo       Start Docker Desktop first, wait for the whale icon to turn steady,
  echo       then run this script again.
  echo.
  pause
  exit /b 1
)
echo   [OK] Docker is running.

echo.
echo [2/3] Starting containers: mysql / redis / mailpit / backend + chat-model / embed-model
docker compose --profile ai up -d
if errorlevel 1 (
  echo   [X] docker compose failed. Scroll up for the reason.
  echo.
  pause
  exit /b 1
)

echo.
echo [3/3] Starting frontend dev server in a new window...
start "footprint-web" /d "%~dp0web" cmd /k "npm run dev"

echo.
echo === Done ===
echo   frontend   http://localhost:5173     ^<- open this one
echo   backend    http://localhost:8000/docs
echo   mailpit    http://localhost:8025     ^(login codes arrive here^)
echo.
echo Notes:
echo   - The footprint-web window stays open running Vite. Close it to stop the frontend.
echo   - To stop containers:  docker compose --profile ai down
echo   - After changing backend Python code, rebuild:  docker compose --profile ai up -d --build backend
echo.
pause
