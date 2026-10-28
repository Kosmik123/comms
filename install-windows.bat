@echo off
rem Creates comms.bat in a folder that is already in PATH, pointing at comms.py from this repo.
rem Usage: install-windows.bat [target_dir]
rem Default target: %LOCALAPPDATA%\Microsoft\WindowsApps (in PATH by default on Windows 10/11).
setlocal
if "%~1"=="" (set "TARGET=%LOCALAPPDATA%\Microsoft\WindowsApps") else (set "TARGET=%~1")
if not exist "%TARGET%\" (
    echo Folder does not exist: %TARGET%
    exit /b 1
)
> "%TARGET%\comms.bat" echo @python "%~dp0comms.py" %%*
if errorlevel 1 (
    echo Failed to write %TARGET%\comms.bat
    exit /b 1
)
echo Created %TARGET%\comms.bat -^> %~dp0comms.py
setlocal enabledelayedexpansion
set "P=;!PATH!;"
if "!P:;%TARGET%;=!"=="!P!" echo WARNING: %TARGET% is not in PATH.
endlocal
endlocal
