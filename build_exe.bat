@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYTHON=py"
) else (
    set "PYTHON=python"
)

%PYTHON% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error

%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed --name GraphWalkerPruner pruner_gui.py
if errorlevel 1 goto :error

echo.
echo Build completed successfully.
echo Executable: %CD%\dist\GraphWalkerPruner.exe
pause
exit /b 0

:error
echo.
echo Build failed. Review the messages above.
pause
exit /b 1
