@echo off
setlocal
if not defined VIRTUAL_ENV goto trypython
if not exist "%VIRTUAL_ENV%\Scripts\python.exe" goto trypython
"%VIRTUAL_ENV%\Scripts\python.exe" "%~dp0fpq-replay.pyz" %*
exit /b %errorlevel%
:trypython
where python >nul 2>nul
if errorlevel 1 goto trypy
python "%~dp0fpq-replay.pyz" %*
exit /b %errorlevel%
:trypy
py -3 "%~dp0fpq-replay.pyz" %*
