@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title wk-switch 安装 / 更新

rem ============================================================
rem  wk-switch Windows 一键部署
rem  用法：右键「以管理员身份运行」，或在命令行执行 install.bat
rem  已安装则自动走更新（保留 data 与 config），未安装则全新安装
rem ============================================================

set "APP=wk-switch"
set "REPO=__REPO__"
set "WK_HOME=%LOCALAPPDATA%\%APP%"
if not "%WK_HOME_OVERRIDE%"=="" set "WK_HOME=%WK_HOME_OVERRIDE%"
set "PORT=8765"
if not "%WK_PORT%"=="" set "PORT=%WK_PORT%"
set "TOKEN=qq928820655"
if not "%WK_TOKEN%"=="" set "TOKEN=%WK_TOKEN%"

echo.
echo ============================================
echo   wk-switch 部署
echo   目录：%WK_HOME%
echo ============================================
echo.

rem ---------- 1. 检查 Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if "%PY%"=="" (
  where py >nul 2>&1 && set "PY=py"
)
if "%PY%"=="" (
  echo [X] 没找到 Python。请先安装 Python 3.8+ 并勾选 "Add to PATH"
  echo     下载：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

for /f "delims=" %%v in ('%PY% -c "import sys;print('%%d.%%d'%%sys.version_info[:2])"') do set "PYVER=%%v"
echo [✓] Python %PYVER%

rem ---------- 2. 判断新装还是更新 ----------
set "MODE=install"
if exist "%WK_HOME%\server.py" set "MODE=update"
if /i "%MODE%"=="update" (
  echo [i] 检测到已安装，进入更新模式
) else (
  echo [i] 未检测到安装，进入全新安装
)

rem ---------- 3. 准备目录 ----------
if not exist "%WK_HOME%" mkdir "%WK_HOME%"
if not exist "%WK_HOME%\data" mkdir "%WK_HOME%\data"
if not exist "%WK_HOME%\config" mkdir "%WK_HOME%\config"
if not exist "%WK_HOME%\backups" mkdir "%WK_HOME%\backups"

rem ---------- 4. 停掉正在跑的实例 ----------
if exist "%WK_HOME%\wk.pid" (
  set /p OLDPID=<"%WK_HOME%\wk.pid"
  taskkill /PID !OLDPID! /F >nul 2>&1
  del /q "%WK_HOME%\wk.pid" >nul 2>&1
  echo [i] 已停止旧实例 !OLDPID!
)

rem ---------- 5. 备份旧代码 ----------
if /i "%MODE%"=="update" (
  for /f "tokens=1-4 delims=/ " %%a in ("%date%") do set "D=%%a%%b%%c"
  set "STAMP=%D%-%time:~0,2%%time:~3,2%%time:~6,2%"
  set "STAMP=!STAMP: =0!"
  if exist "%WK_HOME%\wk" (
    powershell -NoProfile -Command "Compress-Archive -Path '%WK_HOME%\wk','%WK_HOME%\web','%WK_HOME%\server.py','%WK_HOME%\main.py' -DestinationPath '%WK_HOME%\backups\code-%STAMP%.zip' -Force" >nul 2>&1
    echo [✓] 旧代码已备份：backups\code-%STAMP%.zip
  )
)

rem ---------- 6. 取最新发布包 ----------
set "TMPZIP=%TEMP%\%APP%-latest.zip"
set "DLURL="
if not "%WK_PACKAGE%"=="" (
  set "DLURL=file:///%WK_PACKAGE%"
  echo [i] 使用本地包：%WK_PACKAGE%
) else if exist "%~dp0server.py" (
  rem 解压后的离线包：脚本和代码在同一目录，直接用本目录，不必联网
  set "LOCALPKG=%~dp0"
  set "LOCALPKG=!LOCALPKG:~0,-1!"
  set "DLURL=dir:!LOCALPKG!"
  echo [i] 检测到本地代码，直接就地安装
) else (
  echo [i] 查询最新版本...
  for /f "usebackq delims=" %%u in (`powershell -NoProfile -Command ^
    "$ProgressPreference='SilentlyContinue';" ^
    "try { $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/%REPO%/releases/latest' -Headers @{'User-Agent'='wk-switch'};" ^
    "$a = $r.assets ^| Where-Object { $_.name -like '*.zip' } ^| Select-Object -First 1;" ^
    "if ($a) { $a.browser_download_url } else { '' } } catch { '' }"`) do set "DLURL=%%u"
)

if "%DLURL%"=="" (
  echo [X] 取不到发布包。检查网络，或把发布包路径写进环境变量 WK_PACKAGE 后重试
  pause
  exit /b 1
)

if "!DLURL:~0,4!"=="dir:" (
  rem 本地目录模式：跳过下载，直接把当前目录当解压源
  set "EXTRACT=!DLURL:~4!"
  goto :have_source
)

echo [i] 下载：%DLURL%
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%DLURL%' -OutFile '%TMPZIP%' -UseBasicParsing"
if not exist "%TMPZIP%" (
  echo [X] 下载失败
  pause
  exit /b 1
)

rem ---------- 7. 解压覆盖代码 ----------
:have_source
if "%EXTRACT%"=="" (
  set "EXTRACT=%TEMP%\%APP%-unpack"
  if exist "!EXTRACT!" rd /s /q "!EXTRACT!"
  powershell -NoProfile -Command "Expand-Archive -Path '%TMPZIP%' -DestinationPath '!EXTRACT!' -Force"
  set "TEMP_SRC=1"
)

set "SRC=%EXTRACT%"
rem 包内如果只有一层目录，进去
if not exist "%EXTRACT%\server.py" (
  for /d %%d in ("%EXTRACT%\*") do (
    if exist "%%d\server.py" set "SRC=%%d"
  )
)
if not exist "%SRC%\server.py" (
  echo [X] 包里没有 server.py，不是本程序的发布包
  pause
  exit /b 1
)

rem 源目录就是安装目录时不能自己删自己。
rem 用 pushd + %CD% 拿规范化绝对路径比较：直接比字符串会被末尾反斜杠和
rem 短路径（8.3 名）坑到，pushd 出来的 %CD% 一定是完整路径且不带尾斜杠。
pushd "%SRC%" 2>nul
set "SRCABS=%CD%"
popd
pushd "%WK_HOME%" 2>nul
set "HOMABS=%CD%"
popd
if /i "%SRCABS%"=="%HOMABS%" (
  echo [i] 当前目录就是安装目录，跳过复制
  goto :after_copy
)

if exist "%WK_HOME%\wk" rd /s /q "%WK_HOME%\wk"
if exist "%WK_HOME%\web" rd /s /q "%WK_HOME%\web"
xcopy "%SRC%\*" "%WK_HOME%\" /E /Y /Q >nul
:after_copy
rem 只在临时解压目录时才清理，本地目录模式不能删源
if not "%TEMP_SRC%"=="" rd /s /q "%EXTRACT%" >nul 2>&1
if exist "%TMPZIP%" del /q "%TMPZIP%" >nul 2>&1
echo [✓] 代码已更新（data 与 config 未动）

rem ---------- 8. 写配置 ----------
if not exist "%WK_HOME%\config\auth.json" (
  > "%WK_HOME%\config\auth.json" echo {
  >>"%WK_HOME%\config\auth.json" echo   "token": "%TOKEN%",
  >>"%WK_HOME%\config\auth.json" echo   "session_hours": 12
  >>"%WK_HOME%\config\auth.json" echo }
  echo [✓] 已写入访问口令 config\auth.json
) else (
  echo [i] 保留已有口令 config\auth.json
)

if not exist "%WK_HOME%\config\update.json" (
  > "%WK_HOME%\config\update.json" echo {
  >>"%WK_HOME%\config\update.json" echo   "repo": "%REPO%",
  >>"%WK_HOME%\config\update.json" echo   "api": "https://api.github.com",
  >>"%WK_HOME%\config\update.json" echo   "asset": "^wk-switch-[0-9.]+zip$",
  >>"%WK_HOME%\config\update.json" echo   "index_url": "",
  >>"%WK_HOME%\config\update.json" echo   "timeout": 30,
  >>"%WK_HOME%\config\update.json" echo   "allow_prerelease": false
  >>"%WK_HOME%\config\update.json" echo }
)

rem ---------- 9. 生成启动 / 停止脚本 ----------
> "%WK_HOME%\start.bat" echo @echo off
>>"%WK_HOME%\start.bat" echo cd /d "%%~dp0"
>>"%WK_HOME%\start.bat" echo if exist wk.pid (
>>"%WK_HOME%\start.bat" echo   set /p P=<wk.pid
>>"%WK_HOME%\start.bat" echo   tasklist /FI "PID eq %%P" ^| find "python" ^>nul ^&^& (echo 已在运行 PID %%P ^& exit /b 0^)
>>"%WK_HOME%\start.bat" echo )
>>"%WK_HOME%\start.bat" echo start "wk-switch" /min %PY% "%%~dp0main.py" %PORT% --no-browser
>>"%WK_HOME%\start.bat" echo timeout /t 2 ^>nul
>>"%WK_HOME%\start.bat" echo powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -EA SilentlyContinue; if($c){ $c.OwningProcess ^| Set-Content -Path '%%~dp0wk.pid' ; '已启动 PID ' + $c.OwningProcess } else { '启动失败，端口未监听' }"
>>"%WK_HOME%\start.bat" echo start "" http://127.0.0.1:%PORT%

> "%WK_HOME%\stop.bat" echo @echo off
>>"%WK_HOME%\stop.bat" echo powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -EA SilentlyContinue; if($c){ $c ^| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }; '已停止' } else { '没有在跑' }"
>>"%WK_HOME%\stop.bat" echo if exist "%%~dp0wk.pid" del /q "%%~dp0wk.pid"

rem ---------- 10. 启动 ----------
call "%WK_HOME%\start.bat"

echo.
echo ============================================
echo   完成（%MODE%）
echo   目录：%WK_HOME%
echo   地址：http://127.0.0.1:%PORT%
echo   口令：%TOKEN%
echo   启动：%WK_HOME%\start.bat
echo   停止：%WK_HOME%\stop.bat
echo ============================================
pause
