#!/bin/bash
# Apple Books PDF pipeline - macOS launcher.
# Double-click this file in Finder. It builds its own environment on first run
# and leaves nothing outside this folder.
set -u
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" || exit 1

# A double-clicked .command does not always inherit the Homebrew PATH, so add
# both Homebrew bin folders (Apple Silicon and Intel) for python3 and qpdf.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# Optional: a folder given as the first argument becomes the output folder
# (e.g.  "Convert to PDF.command" ~/Downloads/Comics). Default is
# _Chunked_Apple_Books_PDFs in this folder.
if [ -n "${1:-}" ]; then
  export MANGA_OUTPUT="$1"
fi

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
fi

# Install (or finish installing, if a first run was interrupted) the deps.
if ! "$PY" -c "import PIL, reportlab" >/dev/null 2>&1; then
  echo "Installing Pillow and ReportLab..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install pillow reportlab || {
    echo
    echo "ERROR: could not install Pillow and ReportLab. Check your internet connection and run again."
    echo
    read -r -p "Press return to close." _
    exit 1
  }
  echo "Setup complete."
  echo
fi

if ! command -v qpdf >/dev/null 2>&1; then
  echo "WARNING: qpdf was not found on PATH."
  echo "It is needed to merge the chunk PDFs, so the converter will stop without writing anything."
  echo "Install it with:  brew install qpdf"
  echo
fi

echo "Running converter..."
echo
# caffeinate keeps the Mac awake for the duration of a long batch.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i "$PY" -u make_all_chunked_apple_books_pdfs.py
else
  "$PY" -u make_all_chunked_apple_books_pdfs.py
fi
CODE=$?

echo
if [ $CODE -eq 0 ]; then
  echo "Done. Output is in ${MANGA_OUTPUT:-_Chunked_Apple_Books_PDFs}."
else
  echo "Finished with errors. Exit code: $CODE"
fi
read -r -p "Press return to close." _
