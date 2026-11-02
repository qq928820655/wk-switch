@echo off
chcp 65001 >nul
setlocal

set "PORT=8765"

rem 按端口找进程：比按进程名杀安全，不会误伤别的 python
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -EA SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }; '已停止（端口 %PORT%）' } else { '没有在跑' }"

if exist "%~dp0wk.pid" del /q "%~dp0wk.pid" >nul 2>&1
