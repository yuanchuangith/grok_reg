@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [ERROR] uv is not installed or not available in PATH.
  echo Install uv first, then run this launcher again.
  pause
  exit /b 1
)

echo Starting Grok Account Studio...
uv run python -u web_dashboard.py --open

if errorlevel 1 (
  echo.
  echo Dashboard exited with an error.
  pause
)
endlocal
