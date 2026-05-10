@echo off
REM ==========================================================================
REM  Build a portable single-file .exe of the KEF LSX II Controller
REM  Output: dist\KEF LSX Controller.exe
REM ==========================================================================

setlocal
cd /d "%~dp0"

echo.
echo === Installing/updating PyInstaller and dependencies ===
py -3.14 -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail
py -3.14 -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo === Building executable ===
py -3.14 -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --noconsole ^
    --name "KEF LSX Controller" ^
    --icon "Kef-LSX-HP.ico" ^
    --add-data "Kef-LSX-HP.ico;." ^
    --collect-data customtkinter ^
    --hidden-import pystray._win32 ^
    main.py
if errorlevel 1 goto :fail

REM Clean intermediate artifacts (keep dist\)
rmdir /s /q build 2>nul
del /q "KEF LSX Controller.spec" 2>nul

echo.
echo ==========================================================================
echo  BUILD OK
echo  Executable: %CD%\dist\KEF LSX Controller.exe
echo ==========================================================================
echo.
pause
exit /b 0

:fail
echo.
echo ==========================================================================
echo  BUILD FAILED
echo ==========================================================================
echo.
pause
exit /b 1
