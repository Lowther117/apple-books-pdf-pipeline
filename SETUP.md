# Setting it up

Everything here is about getting the folders right. That is the only part that
catches people out — the rest runs itself.

## 1. What you need

**Python 3.9 or newer.** The launcher builds its own environment and installs
Pillow and ReportLab into it. Nothing is installed system-wide.

- Windows: `winget install -e --id Python.Python.3.12`
- macOS: `brew install python`

**qpdf**, for merging the chunk files into one PDF per book.

- Windows: `winget install -e --id QPDF.QPDF`
- macOS: `brew install qpdf`

Without qpdf the run stops straight away with an install hint and converts
nothing — the chunk PDFs are temporary and are only useful once merged. On
Windows, open a new window after installing so the updated PATH is picked up.

## 2. The folder structure

This is the bit that matters. The script expects **exactly two levels** below the
folder it lives in:

```
Apple Books/                         <- the script lives here
├── make_all_chunked_apple_books_pdfs.py
├── Convert Manga.bat
├── Convert to PDF.command
│
├── MangaDex/                        <- LEVEL 1: a source
│   ├── Series A/                    <- LEVEL 2: a book -> one PDF
│   │   ├── 001.jpg
│   │   ├── 002.jpg
│   │   └── ...
│   └── Series B/                    <- another book -> another PDF
│       └── ...
│
├── MangaRead/                       <- another source, same shape
│   └── Series C/
│       └── ...
│
└── _Chunked_Apple_Books_PDFs/       <- created for you; the output lands here
```

**Level 1 is a source.** Any folder you like — name it after where the images came
from, or anything else. You can have as many as you want.

**Level 2 is a book.** Each one becomes exactly one PDF, named after the folder.

**Images can be nested as deep as you like inside a book.** Chapter subfolders are
fine and are the normal case:

```
MangaDex/
└── Series A/
    ├── Vol.01 Ch.0001/
    │   ├── 01.jpg
    │   └── 02.jpg
    └── Vol.01 Ch.0002/
        └── 01.jpg
```

All of that becomes one `Series A.pdf`, with the chapters in order.

**A `mangas` folder is also fine.** Downloaders such as Mihon/Tachiyomi put the
sources inside a `mangas` folder next to a `thumbnails` folder. If the script finds
a `mangas` folder beside it, it reads the sources from in there, ignores
`thumbnails`, and still writes the PDFs next to the script:

```
Apple Books/
├── make_all_chunked_apple_books_pdfs.py
├── mangas/
│   ├── MangaDex (EN)/
│   │   └── Series A/...
│   └── Bato (EN)/
│       └── Series B/...
├── thumbnails/                      <- ignored
└── _Chunked_Apple_Books_PDFs/       <- output
```

### What gets skipped

Folders are ignored at either level if they:

- start with a dot (`.venv`, `.git`, anything hidden), or
- start with an underscore (`_Chunked_Apple_Books_PDFs`, `_anything`)

That is how the output folder avoids being picked up as a source on the next run.
It is also the easiest way to park a book you do not want converted yet: rename it
`_Series D` and it is skipped.

### The mistake to avoid

Putting images directly inside a source folder does not work:

```
MangaDex/
├── 001.jpg     <- WRONG: no book folder, so nothing is converted
└── 002.jpg
```

Every image needs to sit under `Source / Book /`. If a run reports
"Skipped (no images)", this is almost always why.

## 3. Page order

Images are sorted by **natural order**, so numbers sort the way a person expects:

```
1.jpg, 2.jpg, 9.jpg, 10.jpg, 11.jpg      not  1, 10, 11, 2, 9
```

The whole path is sorted folder by folder, not just the filename, so `Ch.0002`
reliably follows `Ch.0001` even when the numbering inside each chapter restarts
at 1, and `Ch 10` comes before `Ch 10.5`.

**Chapter folders sort by chapter number** when the folder name has one in it:
`Chapter 12`, `Ch.12.5`, `Unknown_Chapter 3`, `HAMASCANS_Vol.1 Ch.4 - Title` all
count. This matters for MangaDex-style downloads, where the folder starts with the
scan group's name — without it, a book translated by three groups would read in
group order, not chapter order. If the same chapter number turns up twice (two
groups did it), both are kept, one after the other, and a warning names them so
you can delete the one you do not want.

If pages still come out in the wrong order, the fix is almost always the folder or
file names — zero-pad them (`001`, `002`) and it sorts correctly.

**Site-split images are re-joined.** Bato caps every image at 1500 px tall and
splits anything taller into a 1500 px piece plus the remainder, so a 1313x1850
manga page turns up as `003.webp` (1313x1500) and `004.webp` (1313x350), and an
800x1532 webtoon slice as 800x1500 + 800x32. Left as they are, the remainders
become stray short pages (or, with the old wide-spread rule, get chopped into
columns) and every page is cut in half. The converter spots the pattern - pieces
exactly 1500 px tall (or another common cap) followed by a shorter piece, in the
same chapter and the same width - stacks them back into the original image, and
only then decides how to page it. The log says how many were re-joined per
chapter. An ordinary chapter of equal-height pages with one short credits image
at the end is deliberately left alone.

**Tall webtoon strips are cut at gutters.** A strip more than 2.8x as tall as it
is wide is paged at about 2.2x its width, and each cut is placed at the blank
row band nearest that height (searched back to 55% of it), so panels and speech
bubbles are not sliced through. Consecutive strips in a chapter are paged as one
continuous strip. Where a stretch of art has no gutter at all, the old fixed cut
with 100 px of overlap is used instead; the summary counts both kinds.
`GUTTER_CUTS = False` at the top of the script gives the original fixed cuts.

**File types read:** `.jpg` `.jpeg` `.png` `.webp` `.tif` `.tiff`. Anything else in
the folder is ignored, so stray `.txt`, `.nfo` or `.zip` files do no harm.

## 4. Running it

- **Windows** — double-click `Convert Manga.bat`
- **macOS** — double-click `Convert to PDF.command`

The first run takes a minute longer while it builds its environment. After that it
starts straight away.

On macOS the first double-click may be refused because the file came from the
internet. Either right-click → Open and confirm once, or run
`chmod +x "Convert to PDF.command"` in Terminal.

## 5. What comes out

One PDF per book folder, in `_Chunked_Apple_Books_PDFs/`, named after the folder.
To save elsewhere (your Downloads folder, say): drag that folder onto
`Convert Manga.bat`, or run `./"Convert to PDF.command" ~/Downloads/Comics`, or set
`MANGA_OUTPUT=<folder>`, or run the script directly with `-o <folder>`.
Characters that filenames cannot contain (`< > : " / \ | ? *`) become `-`, and on
Windows a book named after a device (`CON`, `NUL`, `COM1`...) gets a `_` added
(`CON_.pdf`), because Windows will not save a file under that name. If two
books in different sources would produce the same filename, the second gets its
source folder appended (`Series A (MangaRead).pdf`), and `(2)`, `(3)`... after
that, rather than overwriting the first.

**Re-running is safe and cheap.** A book whose PDF already exists is skipped, so
adding one new book and running again only converts the new one. To force a
rebuild, delete that PDF and run again.

To get them onto an iPad: AirDrop the PDF, or drag it into Books on a Mac and let
iCloud sync.

## 6. How pages are handled

Two shapes break naive converters, and both are dealt with automatically:

**Long vertical strips** (webtoons, manhwa) — anything taller than 2.8× its width
is cut into readable pages roughly 2.2× as tall as they are wide, with 100px of
overlap so nothing is lost at the seam.

**Wide spreads** — anything wider than 1.8× its height is cut into pages about
0.9× as wide as they are tall: two pages for a normal double spread, more for
anything wider.

Pages are 720pt wide, capped at 1600pt tall, and images are re-encoded as JPEG at
quality 90. Nothing is resized to A4 and nothing is cropped: the page follows the
image.

### Wide images

The column split for very wide images (`WIDE_SPLIT_ENABLED`) is off by default.
A genuine double-page spread is about 1.4x as wide as tall and never reached the
1.8x trigger, so in practice the rule only ever chopped site banners and credit
strips into three to five columns. A wide image is now simply a short landscape
page. Set `WIDE_SPLIT_ENABLED = True` to get the columns back.

### The one setting manga readers will want

Near the top of the script:

```python
READ_WIDE_IMAGES_RIGHT_TO_LEFT = False
```

This controls the order the two halves of a **wide spread** are written. It is
`False` (left-to-right) by default, which is right for Western comics and for
manhwa. For Japanese manga read right-to-left, set it to `True` — otherwise
double-page spreads will be the wrong way round. It affects nothing else.

## 7. When something goes wrong

**"Skipped (no images)"** — the book folder has no readable images, every image
in it was rejected (unreadable, or smaller than 180px on a side), or the images
are sitting directly in a source folder instead of inside a book folder. See §2.

**"ERROR: qpdf not found"** — the run stops before converting anything. Install
it (§1), open a fresh window and re-run; already-finished books are skipped.

**"UNREADABLE FILES" block at the end of the run** — those files have an image
extension but are not images (no JPEG/PNG/WebP header; usually a download that
came down garbled). Nothing can recover them. Re-download the chapters listed,
delete that book's PDF from `_Chunked_Apple_Books_PDFs`, and run again — only
that book is redone.

**A page is missing from the output** — very large images are skipped rather than
risk the run. Pillow's decompression-bomb limit is deliberately left at its
default. To include them, set `MAX_IMAGE_PIXELS_OVERRIDE = None` near the top of
the script. That does change the output, which is why it is not the default.

**Run is slow, or the machine struggles** — the worker count is one per core,
up to 16, capped at about 2 GB of RAM per worker (available RAM on Windows,
total RAM on macOS), because decoding images is what actually exhausts memory.
If a worker crashes anyway, the run restarts the pool with one worker fewer and
retries the unfinished books. Force a number with the `MANGA_WORKERS` environment variable
(set it in the same Terminal/cmd window, then run the launcher from there):

```
# Windows
set MANGA_WORKERS=4

# macOS
export MANGA_WORKERS=4
```

**Keeping the images somewhere else** — set `MANGA_ROOT` to that folder and the
script reads from there instead of from its own folder:

```
# macOS
export MANGA_ROOT="/Volumes/External/Comics"
```

## 8. Only convert certain sources

By default every eligible top-level folder is treated as a source. To restrict it,
edit near the top of the script:

```python
SOURCE_NAMES: list[str] = ["MangaDex", "MangaRead"]
```

Leave it empty for automatic detection.
