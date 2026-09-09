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
split vertically into two.

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

Without qpdf the chunk files are still written, they just are not merged.

## Layout

```
Apple Books/
  make_all_chunked_apple_books_pdfs.py   the converter
  Convert Manga.bat                      Windows launcher
  Convert to PDF.command                 macOS launcher
  <Source>/<Book>/*.jpg|png|webp|tif     your image folders
  _Chunked_Apple_Books_PDFs/             output, one PDF per book
```

Every top-level folder that is not hidden, not `.venv*` and not prefixed with `_`
is treated as a source; each folder inside it is one book, one PDF. Images are
read in natural sort order, so `9.jpg` comes before `10.jpg`.

Already-converted books are skipped on a re-run, so adding a new book and running
again only does the new work.

## Notes

- Worker count is capped by available RAM rather than core count, and workers are
  recycled, because image decoding is what actually exhausts memory here.
- Formats read: `.jpg` `.jpeg` `.png` `.webp` `.tif` `.tiff`.
- Nothing is resized to A4 and nothing is cropped. Page size follows the image.
- `MANGA_ROOT` can be set as an environment variable to point somewhere other
  than the folder the script lives in.

Only the code is in this repository. The source images and the PDFs it generates
are excluded by `.gitignore` — they are large, and they are not mine to publish.

---

*Built for my own use, in collaboration with AI (Anthropic's Claude). I described the problems, made the decisions and tested the results; Claude wrote much of the code. Shared as-is — a personal fix, not a product. No support and no warranty.*
