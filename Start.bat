@echo off
rem SPMS - Spare Parts Management System
rem Author: Rookie Uncle (@rookie-uncle / @Colton1982)
rem Email: 510658927@qq.com  QQ: 510658927
rem Gitee: https://gitee.com/rookie-uncle
rem GitHub: https://github.com/Colton1982
rem Copyright (c) 2026 rookie-uncle. All rights reserved.
rem License: Apache-2.0
title Spare Parts Management System
cd /d "%~dp0"
set "PY=%~dp0runtime\python.exe"
if exist "%PY%" goto loop
where python >nul 2>nul
if errorlevel 1 goto nopython
set "PY=python"

:loop
echo ============================================================
echo   Spare Parts Management System  -  Starting...
echo   First run: a browser will open for initial setup.
echo   Enter the database file location on the shared disk
echo   (all users point to the same database).
echo   Close this window to stop the server.
echo ============================================================
"%PY%" app.py
rem Exit code 75 means: the app replaced its own database file and asks to be
rem started again. Anything else means the user stopped it, so we stop too.
if errorlevel 75 goto restart
echo.
echo Server stopped.
pause
exit /b 0

:restart
rem The user's browser is already open on this address - do not pop a new tab.
set "SMT_NO_BROWSER=1"
echo.
echo Database was restored. Restarting...
timeout /t 2 /nobreak >nul
cls
goto loop

:nopython
echo [ERROR] runtime\python.exe not found.
echo Please make sure the "runtime" folder is included.
pause
exit /b 0
