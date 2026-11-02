@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PORT=8765"
set "PY=python"
where python >nul 2>&1 || set "PY=py"

if exist wk.pid (
  set /p OLDPID=<wk.pid
  tasklist /FI "PID eq %OLDPID%" 2>nul | find "python" >nul && (
    echo 已在运行，PID %OLDPID%
    start "" http://127.0.0.1:%PORT%
    exit /b 0
  )
)

echo 启动 wk-switch（端口 %PORT%）...
start "wk-switch" /min %PY% "%~dp0main.py" %PORT% --no-browser
timeout /t 2 >nul

rem 用 PowerShell 反查监听端口拿到真实 PID，比 %ERRORLEVEL% 可靠
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -EA SilentlyContinue; if($c){ $c.OwningProcess | Set-Content -Path '%~dp0wk.pid'; '已启动 PID ' + $c.OwningProcess } else { '启动失败：端口未监听，检查 Python 是否可用' }"
start "" http://127.0.0.1:%PORT%
