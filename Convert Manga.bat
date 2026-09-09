@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
title Manga to Apple Books PDFs

REM ---- First run: build the venv and install deps ----
if not exist ".venv\Scripts\python.exe" (
    echo First run detected. Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Could not create venv. Python may not be installed.
        echo Run:  winget install -e --id Python.Python.3.12
        echo Then close and reopen, and run this again.
        echo.
        pause
        exit /b 1
    )
    echo Installing Pillow and ReportLab...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install pillow reportlab
    echo Setup complete.
    echo.
)

REM ---- Run the converter (live progress streams below) ----
echo Running converter...
echo.
".venv\Scripts\python.exe" -u "make_all_chunked_apple_books_pdfs.py"
set EXITCODE=%errorlevel%

echo.
if %EXITCODE%==0 (
    echo Done. Output is in _Chunked_Apple_Books_PDFs.
) else (
    echo Finished with errors. Exit code: %EXITCODE%
)
echo Press any key to close.
pause >nul
