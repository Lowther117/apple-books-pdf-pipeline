# Apple Books PDF pipeline

Turns folders of images into PDFs that behave properly in Apple Books.

Point it at a folder of image folders and it produces one PDF per book, sized so
that Apple Books renders each page edge to edge instead of letterboxing it. Built
for reading long-form comics on an iPad, where the usual routes fall down.

## Why it works this way

Three things kept breaking before this approach settled:

- **EPUB** was the obvious answer and the wrong one. Image-heavy EPUBs render
  inconsistently in Apple Books and the page geometry is not yours to control.
- **`img2pdf`** produced page-size errors on mixed-dimension source images, which
  is every scanned book.
- **A single ReportLab pass** over a long volume got the process killed by macOS
  once memory climbed far enough.

So the pipeline writes temporary chunk PDFs of about 150 pages each and merges
them with `qpdf` at the end. That keeps peak memory flat regardless of book
length, and the merge is lossless.

It also handles the two page shapes that break naive converters: long vertical
strips are split horizontally into readable pages, and double-page spreads are
split vertically into two or more.

## Which file do I use?

| File | OS | What it does |
|---|---|---|
| `Convert Manga.bat` | Windows | Double-click. Builds `.venv` on first run, then runs the converter. |
| `Convert to PDF.command` | macOS | Double-click. Builds `.venv-mac` on first run, then runs the converter (kept awake with `caffeinate`). |
| `make_all_chunked_apple_books_pdfs.py` | both | The converter itself. Run it directly with any Python 3.9+ that has Pillow and ReportLab. |

## Running it

Full setup, the exact folder structure it expects, and troubleshooting are in
[SETUP.md](SETUP.md). The short version:

**Windows** — double-click `Convert Manga.bat`
**macOS** — double-click `Convert to PDF.command`

Each launcher builds its own virtual environment inside this folder on first run
and installs what it needs. Nothing is installed system-wide.

Both need **qpdf** for the final merge:

```
# Windows
winget install -e --id QPDF.QPDF

# macOS
brew install qpdf
```

Without qpdf the converter stops before doing anything and tells you how to
install it (the chunk files are temporary and only make sense merged).

## Layout

```
Apple Books/
  make_all_chunked_apple_books_pdfs.py   the converter
  Convert Manga.bat                      Windows launcher
  Convert to PDF.command                 macOS launcher
  <Source>/<Book>/*.jpg|png|webp|tif     your image folders
  _Chunked_Apple_Books_PDFs/             output, one PDF per book
```

Every top-level folder that is not hidden, not `.venv*`, not `thumbnails` and not
prefixed with `_` is treated as a source; each folder inside it is one book, one
PDF. If there is a `mangas` folder next to the script (the layout the
Mihon/Tachiyomi-style downloaders produce), the sources are read from inside it
instead and the PDFs still land next to the script.

Images are read in natural sort order, so `9.jpg` comes before `10.jpg`, and
`Ch 10` comes before `Ch 10.5`. A chapter folder whose name carries a chapter
number (`Chapter 12`, `Ch.12.5`, `HAMASCANS_Vol.1 Ch.4 - Title`) sorts by that
number, so a book whose chapters came from several scan groups reads in order
rather than grouped by scanlator. The same chapter from two groups is kept
twice, back to back, with a warning.

Already-converted books are skipped on a re-run, so adding a new book and running
again only does the new work.

## Notes

- Books are converted in parallel, one worker per core, up to 16, and capped by
  RAM at about 2 GB per worker: available RAM on Windows, total RAM on macOS
  (so a 16 GB Mac runs at most 8 workers, an 8 GB Mac at most 4). Workers are
  recycled between books (on Python 3.11 or newer) because image decoding is
  what actually exhausts memory here. `MANGA_WORKERS=4` forces a number.
- If a worker process dies outright (out of memory, a crashing image decoder),
  the run carries on with one worker fewer and retries the unfinished books; a
  book that crashes it twice is reported as failed. Any failure makes the run
  exit with code 1, and the launchers say "Finished with errors".
- Transparent PNG/WebP pages are flattened onto white.
- Thin remainder strips (the 800x32 slivers some sites put between 800x1500
  webtoon pages) are glued onto the same-width page next to them rather than
  dropped. Anything under 180 px in both directions with no same-width
  neighbour is still skipped.
- Files that are not valid images (garbled downloads) are skipped and listed by
  book and chapter at the end of the run, so you know what to re-download.
- Formats read: `.jpg` `.jpeg` `.png` `.webp` `.tif` `.tiff`.
- Nothing is resized to A4 and nothing is cropped. Page size follows the image.
- `MANGA_ROOT` can be set as an environment variable to point somewhere other
  than the folder the script lives in.
- Output goes to `_Chunked_Apple_Books_PDFs` by default. To save somewhere else
  (e.g. Downloads), drop a folder onto `Convert Manga.bat` / pass it to
  `Convert to PDF.command`, set `MANGA_OUTPUT`, or run the script with `-o FOLDER`.

Only the code is in this repository. The source images and the PDFs it generates
are excluded by `.gitignore` — they are large, and they are not mine to publish.

---

*Built for my own use, in collaboration with AI (Anthropic's Claude). I described the problems, made the decisions and tested the results; Claude wrote much of the code. Shared as-is — a personal fix, not a product. No support and no warranty.*

## Licence

MIT No Attribution (MIT-0): do whatever you like with it - no credit needed, no warranty. See `LICENSE`.
