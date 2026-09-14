@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
title Manga to Apple Books PDFs

REM ---- Optional: a folder dropped onto this .bat (or passed as the first
REM ---- argument) becomes the output folder. Default: _Chunked_Apple_Books_PDFs.
if not "%~1"=="" set "MANGA_OUTPUT=%~1"

REM ---- First run: build the venv and install deps ----
if not exist ".venv\Scripts\python.exe" (
    echo First run detected. Creating virtual environment...
    REM Prefer the "py" launcher; fall back to "python" if it is not installed.
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo ERROR: Could not create venv. Python may not be installed.
        echo Run:  winget install -e --id Python.Python.3.12
        echo Then close and reopen, and run this again.
        echo.
        pause
        exit /b 1
    )
)

REM ---- Install the deps (also finishes a first run that was interrupted) ----
".venv\Scripts\python.exe" -c "import PIL, reportlab" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow and ReportLab...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install pillow reportlab
    if errorlevel 1 (
        echo.
        echo ERROR: Could not install Pillow and ReportLab. Check your internet connection and run again.
        echo.
        pause
        exit /b 1
    )
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
    if defined MANGA_OUTPUT (echo Done. Output is in "%MANGA_OUTPUT%".) else (echo Done. Output is in _Chunked_Apple_Books_PDFs.)
) else (
    echo Finished with errors. Exit code: %EXITCODE%
)
echo Press any key to close.
pause >nul
