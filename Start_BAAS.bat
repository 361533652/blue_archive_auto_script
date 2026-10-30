@rem BAAS launcher: sync upstream (official repo) -> push to your fork -> start BAAS.
@rem Keep this file ASCII-only so it works under any console code page.
@echo off
setlocal

set "_root=%~dp0"
set "_root=%_root:~0,-1%"
cd /d "%_root%"

set "BAAS_SYNCED=0"

set "_py=%_root%\.venv\Scripts\python.exe"
if not exist "%_py%" set "_py=python"

echo ============================================================
echo [BAAS] Step 1/2 - sync upstream and push to your fork
echo ============================================================
"%_py%" "%_root%\deploy\sync_upstream.py"
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] Upstream merge conflict - startup blocked.
    echo Report: log\sync_upstream_conflict.log
    echo Resolve the conflicts, then run this launcher again.
    echo ============================================================
    echo.
    pause
    exit /b 1
)
set "BAAS_SYNCED=1"

echo.
echo ============================================================
echo [BAAS] Step 2/2 - start BAAS
echo ============================================================
set "_exe="
for %%f in ("%_root%\BlueArchiveAutoScript*.exe") do set "_exe=%%~ff"

if defined _exe (
    echo [BAAS] Launching "%_exe%" ...
    start "" "%_exe%"
    exit /b 0
)

echo [BAAS] Launcher exe not found, starting window.py directly ...
"%_py%" "%_root%\window.py"
exit /b %errorlevel%
