#!/bin/bash
# Apple Books PDF pipeline - macOS launcher.
# Double-click this file in Finder. It builds its own environment on first run
# and leaves nothing outside this folder.
set -u
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" || exit 1

VENV=".venv-mac"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "First run: creating the virtual environment..."
  python3 -m venv "$VENV" || {
    echo
    echo "ERROR: could not create the environment. Python 3 may not be installed."
    echo "Install it with:  brew install python"
    echo
    read -r -p "Press return to close." _
    exit 1
  }
  echo "Installing Pillow and ReportLab..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install pillow reportlab
  echo "Setup complete."
  echo
fi

if ! command -v qpdf >/dev/null 2>&1; then
  echo "WARNING: qpdf was not found on PATH."
  echo "The chunk files will be written but not merged into one PDF per book."
  echo "Install it with:  brew install qpdf"
  echo
fi

echo "Running converter..."
echo
"$PY" -u make_all_chunked_apple_books_pdfs.py
CODE=$?

echo
if [ $CODE -eq 0 ]; then
  echo "Done. Output is in _Chunked_Apple_Books_PDFs."
else
  echo "Finished with errors. Exit code: $CODE"
fi
read -r -p "Press return to close." _
