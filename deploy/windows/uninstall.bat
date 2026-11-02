@echo off
chcp 65001 >nul
setlocal
title wk-switch 卸载

set "WK_HOME=%LOCALAPPDATA%\wk-switch"
if not "%WK_HOME_OVERRIDE%"=="" set "WK_HOME=%WK_HOME_OVERRIDE%"

echo.
echo 将停止服务并删除程序文件：%WK_HOME%
echo data\ 与 config\ 会保留（账号库和口令在里面）。
echo.
choice /c YN /m "继续卸载"
if errorlevel 2 exit /b 0

call "%~dp0stop.bat"
if exist "%WK_HOME%\wk" rd /s /q "%WK_HOME%\wk"
if exist "%WK_HOME%\web" rd /s /q "%WK_HOME%\web"
del /q "%WK_HOME%\main.py" "%WK_HOME%\server.py" "%WK_HOME%\start.bat" "%WK_HOME%\stop.bat" 2>nul

echo [OK] 已卸载。数据保留在 %WK_HOME%\data，配置保留在 %WK_HOME%\config
pause
