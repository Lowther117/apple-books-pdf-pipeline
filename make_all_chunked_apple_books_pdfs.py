#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_all_chunked_apple_books_pdfs.py

One converter for both launchers ("Convert Manga.bat" on Windows, "Convert to
PDF.command" on macOS). It grew out of the proven Mac-only converter; the
per-book conversion logic is identical to that original: same page geometry,
same tall/wide splitting, same encoding, same ordering.

The ONLY intentional differences from the Mac original are output-neutral and
either required on Windows or requested:
  * multiprocessing uses "spawn" (Windows has no "fork"), so all real work is
    under `if __name__ == "__main__":` with mp.freeze_support().
  * qpdf is located on PATH, in its standard Windows install folders, or in
    the Homebrew bin folders on macOS.
  * worker count is RAM-capped (CPU cores / available RAM / hard cap) instead
    of one-per-core, and workers are recycled to return memory to the OS.
  * a Windows keep-awake call replaces macOS `caffeinate`.
  * a little extra progress printing.
None of these can change a single pixel of the output PDFs.

Later bug fixes that DO change output, but only for the books they affect:
  * natural sort compares folder by folder ("Ch 10" before "Ch 10.5");
  * the _PDFs/_CBZ/_EPUBs/_Apple_Books filters look only inside the book, so a
    book or root folder with one of those in its name is no longer skipped;
  * transparent PNG/WebP pages are flattened onto white instead of black;
  * chapter folders sort by their chapter NUMBER when the name contains one
    ("HAMASCANS_Vol.1 Ch.4", "Chapter 1.5", "Unknown_Chapter 3"), so books
    whose chapters came from several scan groups no longer sort by group name;
  * thin remainder strips (e.g. the 800x32 slivers Bato puts between 800x1500
    webtoon pages) are glued onto the same-width page next to them instead of
    being skipped, so no content is lost;
  * files that are not valid images (garbled downloads) are listed by chapter
    at the end of the run so you know what to re-download.
Opaque pages in correctly named folders come out exactly as before.

The sources may sit directly beside this script, or inside a "mangas" subfolder
(as the Mihon/Tachiyomi-style downloaders lay them out); both are detected.
"""

import os
import re
import sys
import gc
import math
import time
import shutil
import tempfile
import subprocess
from io import BytesIO
from pathlib import Path
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


# ----------------------------------------------------------------------------
# Batch configuration  (identical to the original unless noted)
# ----------------------------------------------------------------------------
# Root that contains the per-source folders (Source / Book / [Chapter] / images).
# Defaults to the "mangas" subfolder next to this script when one exists, else
# the folder this script lives in. Override with the MANGA_ROOT env var.
_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_ROOT = _SCRIPT_DIR / "mangas" if (_SCRIPT_DIR / "mangas").is_dir() else _SCRIPT_DIR
MANGA_ROOT = Path(os.environ.get("MANGA_ROOT", str(_DEFAULT_ROOT)))

# Leave empty to auto-detect every top-level folder under MANGA_ROOT (ignoring
# hidden, ".venv", and "_..." folders). Each immediate subfolder of a source
# folder is one book -> one PDF.
SOURCE_NAMES: list[str] = []  # e.g. ["MangaDex", "MangaRead", "ManhuaPlus"]

# Where the PDFs go. Default: "_Chunked_Apple_Books_PDFs" next to the sources.
# Override with the MANGA_OUTPUT env var (the launchers pass through a folder
# dropped on / given to them) or with --output / -o on the command line.
# When the root is the auto-detected "mangas" subfolder the PDFs go next to the
# script, not inside "mangas".
_OUTPUT_PARENT = _SCRIPT_DIR if MANGA_ROOT == _SCRIPT_DIR / "mangas" else MANGA_ROOT
OUTPUT_ROOT = Path(os.environ.get("MANGA_OUTPUT") or str(_OUTPUT_PARENT / "_Chunked_Apple_Books_PDFs")).expanduser()

# Skip books whose output PDF already exists (resume / add-new without redoing).
SKIP_EXISTING_OUTPUT = True

# Folders never treated as a source or a book.
IGNORED_TOP_LEVEL = {".venv", ".venv-mac", "thumbnails"}  # "thumbnails": downloader covers

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
}

# Apple Books-friendly page settings
PDF_PAGE_WIDTH = 720
PDF_MAX_PAGE_HEIGHT = 1600

# Long thin webtoon/manhwa page handling
TALL_ASPECT_TRIGGER = 2.8
TALL_SLICE_HEIGHT_AS_WIDTH_RATIO = 2.2
OVERLAP_PX = 100

# Very wide spread/outlier page handling
WIDE_ASPECT_TRIGGER = 1.8
WIDE_SLICE_WIDTH_AS_HEIGHT_RATIO = 0.9
READ_WIDE_IMAGES_RIGHT_TO_LEFT = False

# Stability settings
JPEG_QUALITY = 90
MAX_PDF_PAGES_PER_CHUNK = 150

MIN_SLICE_HEIGHT_PX = 180
MIN_SLICE_WIDTH_PX = 180

# --- Windows-only knobs (do NOT affect output) ------------------------------
# Worker count = smallest of: CPU cores, available RAM / RAM_PER_WORKER_GB,
# WORKER_HARD_CAP. Override entirely with the MANGA_WORKERS env var.
RAM_PER_WORKER_GB = 2.0
WORKER_HARD_CAP = 16
# Recycle each worker after this many books so memory returns to the OS on long
# runs (needs Python 3.11+). None = never recycle.
TASKS_PER_WORKER = 1
# Console: print a within-book progress line every this many source images.
PROGRESS_EVERY = 100
# Leave Pillow's default decompression-bomb limit in place to match the Mac
# output exactly (the same oversized pages are skipped the same way). Set to
# None to lift the limit (includes those pages -- an OUTPUT-CHANGING choice),
# or to an int for a custom pixel ceiling. "default" means do not touch it.
MAX_IMAGE_PIXELS_OVERRIDE = "default"


if MAX_IMAGE_PIXELS_OVERRIDE != "default":
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS_OVERRIDE


# ----------------------------------------------------------------------------
# Windows helpers (qpdf discovery, RAM, keep-awake) -- output-neutral
# ----------------------------------------------------------------------------

def find_qpdf() -> "str | None":
    """Locate qpdf on PATH, else in standard install folders. None if missing."""
    exe = shutil.which("qpdf")
    if exe:
        return exe

    # macOS: a double-clicked .command may not have Homebrew on PATH.
    for candidate in ("/opt/homebrew/bin/qpdf", "/usr/local/bin/qpdf"):
        if os.access(candidate, os.X_OK):
            return candidate

    import glob
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "qpdf*", "**", "qpdf.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "qpdf*", "**", "qpdf.exe"),
    ]
    if local:
        patterns.append(os.path.join(local, "Microsoft", "WinGet", "Packages", "*QPDF*", "**", "qpdf.exe"))
        patterns.append(os.path.join(local, "qpdf*", "**", "qpdf.exe"))

    found = []
    for pat in patterns:
        found += glob.glob(pat, recursive=True)
    if found:
        found.sort(key=lambda p: natural_key(Path(p)), reverse=True)  # newest
        return found[0]
    return None


def available_ram_gb() -> float:
    """Best-effort physical RAM in GB for the worker cap; 0.0 if unknown.

    Windows: available RAM (GlobalMemoryStatusEx). macOS: TOTAL RAM
    (`sysctl -n hw.memsize`), because macOS has no simple "available" figure
    and counts compressed/cached memory as in use. Linux: MemAvailable.
    """
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5, stdin=subprocess.DEVNULL)
            return int(out.stdout.strip()) / (1024 ** 3)
        except Exception:
            return 0.0
    if sys.platform != "win32":
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024 ** 2)
        except Exception:
            pass
        try:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
        except Exception:
            return 0.0
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullAvailPhys / (1024 ** 3)
    except Exception:
        return 0.0


def decide_workers() -> int:
    """Pick a safe parallel worker count (RAM-capped)."""
    env = os.environ.get("MANGA_WORKERS")
    if env:
        try:
            return max(1, int(env))  # explicit override wins
        except ValueError:
            pass
    workers = os.cpu_count() or 4
    avail = available_ram_gb()
    if avail > 0 and RAM_PER_WORKER_GB > 0:
        workers = min(workers, max(1, int(avail / RAM_PER_WORKER_GB)))
    if WORKER_HARD_CAP:
        workers = min(workers, WORKER_HARD_CAP)
    return max(1, workers)


def set_keep_awake(enable: bool):
    """Keep Windows awake during the run (the equivalent of macOS caffeinate)."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        flags = ES_CONTINUOUS
        if enable:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Conversion core  (the Mac original, plus the three fixes listed at the top)
# ----------------------------------------------------------------------------

def natural_key(path: Path):
    # Compare folder by folder, not as one string, so "Ch 10/001.jpg" sorts
    # before "Ch 10.5/001.jpg" (as one string "/" vs "." put 10.5 first).
    # re.split with a capture group always alternates text, digits, text...,
    # so ints are only ever compared with ints and strings with strings.
    return [
        [int(bit) if bit.isdecimal() else bit for bit in re.split(r"(\d+)", part.lower())]
        for part in Path(path).parts
    ]


_WINDOWS_DEVICE_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                         | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "-")
    name = name.strip()
    # Windows will not create a file named after a device (CON, NUL, COM1...),
    # with any extension and with spaces before the first dot: "CON.pdf" and
    # "nul .x.pdf" fail to save. Only those names change ("CON_.pdf").
    if sys.platform == "win32":
        head, dot, tail = name.partition(".")
        if head.rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES:
            name = head + "_" + dot + tail
    return name


def is_image(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and not path.name.startswith(".")
    )


def _excluded(rel: str) -> bool:
    return any(tag in rel for tag in ("_Apple_Books", "_PDFs", "_EPUBs", "_CBZ"))


# Chapter number inside a folder name: "Chapter 12", "Ch.12.5", "Unknown_Chapter 3",
# "HAMASCANS_Vol.1 Ch.4 - Title". The lookbehind stops "Schale"/"Chamber" matching.
_CHAPTER_RE = re.compile(r"(?<![a-z])ch(?:apter)?\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def chapter_number(folder_name: str) -> "float | None":
    m = _CHAPTER_RE.search(folder_name)
    return float(m.group(1)) if m else None


def reading_order_key(rel: Path):
    """Sort key for a path relative to the book folder. A folder whose name
    carries a chapter number sorts by that number (so "Group_Vol.3 Ch.15" sorts
    by 15, not by the group's name); everything else keeps natural order.
    Each element is (kind, chapter, natural) so ints never meet strings."""
    key = []
    for part in rel.parts[:-1]:
        ch = chapter_number(part)
        key.append((0, ch, natural_key(Path(part))) if ch is not None
                   else (1, 0.0, natural_key(Path(part))))
    key.append((1, 0.0, natural_key(Path(rel.parts[-1]))))
    return key


def warn_duplicate_chapters(book_folder: Path):
    """The same chapter from two scan groups: both are kept, back to back."""
    seen: dict[float, list[str]] = {}
    for entry in book_folder.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            ch = chapter_number(entry.name)
            if ch is not None:
                seen.setdefault(ch, []).append(entry.name)
    for ch, names in sorted(seen.items()):
        if len(names) > 1:
            print(f"  WARNING: {book_folder.name}: chapter {ch:g} appears {len(names)} times "
                  f"(all kept, back to back): " + " | ".join(sorted(names)), flush=True)


def collect_images(book_folder: Path):
    # The output-folder filters look only at the path INSIDE the book, so a
    # root such as ".../My_PDFs" or a book named "Book_CBZ_fix" is not skipped.
    images = [
        p for p in book_folder.rglob("*")
        if is_image(p)
        and not _excluded(str(p.relative_to(book_folder)))
    ]
    return sorted(images, key=lambda p: reading_order_key(p.relative_to(book_folder)))


class ChunkedPDFWriter:
    def __init__(self, chunk_dir: Path):
        self.chunk_dir = chunk_dir
        self.chunk_dir.mkdir(parents=True, exist_ok=True)

        self.chunk_files = []
        self.current_pdf = None
        self.current_chunk_page_count = 0
        self.total_page_count = 0
        self.chunk_number = 0

        self.start_new_chunk()

    def start_new_chunk(self):
        if self.current_pdf is not None:
            self.current_pdf.save()
            self.current_pdf = None
            gc.collect()

        self.chunk_number += 1
        self.current_chunk_page_count = 0

        chunk_file = self.chunk_dir / f"chunk_{self.chunk_number:05d}.pdf"
        self.chunk_files.append(chunk_file)

        self.current_pdf = canvas.Canvas(str(chunk_file), pageCompression=1)

    def add_page(self, im: Image.Image):
        width, height = im.size

        if width < MIN_SLICE_WIDTH_PX or height < MIN_SLICE_HEIGHT_PX:
            print(f"  WARNING: skipping tiny page {width}x{height}")
            return

        if self.current_chunk_page_count >= MAX_PDF_PAGES_PER_CHUNK:
            self.start_new_chunk()

        page_width = PDF_PAGE_WIDTH
        page_height = PDF_PAGE_WIDTH * (height / width)

        if page_height > PDF_MAX_PAGE_HEIGHT:
            page_height = PDF_MAX_PAGE_HEIGHT

        buf = BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        buf.seek(0)

        self.current_pdf.setPageSize((page_width, page_height))
        self.current_pdf.drawImage(
            ImageReader(buf),
            0,
            0,
            width=page_width,
            height=page_height,
            preserveAspectRatio=True,
            anchor="sw",
        )
        self.current_pdf.showPage()

        buf.close()

        self.current_chunk_page_count += 1
        self.total_page_count += 1

    def close(self):
        if self.current_pdf is not None:
            self.current_pdf.save()
            self.current_pdf = None
            gc.collect()


def split_tall(writer: ChunkedPDFWriter, im: Image.Image, src_name: str):
    width, height = im.size

    target_slice_height = int(width * TALL_SLICE_HEIGHT_AS_WIDTH_RATIO)
    max_slice_height_by_pdf = int(width * (PDF_MAX_PAGE_HEIGHT / PDF_PAGE_WIDTH))

    target_slice_height = min(target_slice_height, max_slice_height_by_pdf)
    target_slice_height = max(target_slice_height, MIN_SLICE_HEIGHT_PX)

    step = max(1, target_slice_height - OVERLAP_PX)
    approx_pages = math.ceil(height / step)

    print(f"  Tall split: {src_name} ({width}x{height}) into about {approx_pages} PDF pages")

    y = 0

    while y < height:
        bottom = min(y + target_slice_height, height)

        remaining = height - bottom
        if 0 < remaining < MIN_SLICE_HEIGHT_PX:
            bottom = height

        slice_height = bottom - y

        if slice_height < MIN_SLICE_HEIGHT_PX:
            break

        crop = im.crop((0, y, width, bottom))
        writer.add_page(crop)
        crop.close()

        if bottom >= height:
            break

        y += step


def split_wide(writer: ChunkedPDFWriter, im: Image.Image, src_name: str):
    width, height = im.size

    target_slice_width = int(height * WIDE_SLICE_WIDTH_AS_HEIGHT_RATIO)
    target_slice_width = max(target_slice_width, MIN_SLICE_WIDTH_PX)

    slices = max(2, math.ceil(width / target_slice_width))
    actual_slice_width = width / slices

    print(f"  Wide split: {src_name} ({width}x{height}) into {slices} PDF pages")

    indexes = list(range(slices))

    if READ_WIDE_IMAGES_RIGHT_TO_LEFT:
        indexes = list(reversed(indexes))

    for i in indexes:
        left = int(round(i * actual_slice_width))
        right = int(round((i + 1) * actual_slice_width))

        if right - left < MIN_SLICE_WIDTH_PX:
            continue

        crop = im.crop((left, 0, right, height))
        writer.add_page(crop)
        crop.close()


def load_image(image_path: Path, stats: dict) -> "Image.Image | None":
    """Open, EXIF-rotate, flatten transparency onto white and convert to RGB.
    Returns None (and records the file) if it is not a readable image."""
    try:
        with Image.open(image_path) as src:
            src.load()
            im = ImageOps.exif_transpose(src)

            # Transparent pages: flatten onto white (a plain RGB convert turns
            # transparent areas black). Opaque images do not take this branch.
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
                im = flat

            return im.convert("RGB")
    except Exception as e:
        print(f"  WARNING: could not read image: {image_path}")
        print(f"  Reason: {e}")
        stats["unreadable"].append(str(image_path))
        return None


def place_image(writer: ChunkedPDFWriter, im: Image.Image, src_name: str, stats: dict):
    """The original per-image geometry logic: tall split / wide split / one page."""
    width, height = im.size

    if width < MIN_SLICE_WIDTH_PX or height < MIN_SLICE_HEIGHT_PX:
        print(f"  WARNING: skipping tiny source image {src_name} ({width}x{height})")
        stats["tiny_skipped"].append(src_name)
        return

    aspect_tall = height / width
    aspect_wide = width / height

    if aspect_tall >= TALL_ASPECT_TRIGGER:
        split_tall(writer, im, src_name)
    elif aspect_wide >= WIDE_ASPECT_TRIGGER:
        split_wide(writer, im, src_name)
    else:
        writer.add_page(im)


def vstack(top: Image.Image, bottom: Image.Image) -> Image.Image:
    out = Image.new("RGB", (top.width, top.height + bottom.height), (255, 255, 255))
    out.paste(top, (0, 0))
    out.paste(bottom, (0, top.height))
    return out


def is_thin_strip(im: Image.Image) -> bool:
    return im.height < MIN_SLICE_HEIGHT_PX and im.width >= MIN_SLICE_WIDTH_PX


class StripGluer:
    """Some sources (Bato webtoons) deliver 800x1500 pages with an 800x32
    remainder strip between each. Those strips are real content, so instead of
    skipping them a thin strip is glued onto the same-width image before it
    (or, if it comes first, onto the image after it). Pagination is unchanged
    apart from the glued-on sliver. Strips of a different width still skip."""

    def __init__(self, writer: ChunkedPDFWriter, stats: dict):
        self.writer = writer
        self.stats = stats
        self.pending: "Image.Image | None" = None
        self.pending_name = ""

    def push(self, im: Image.Image, name: str):
        if self.pending is not None and im.width == self.pending.width and (
            is_thin_strip(im) or is_thin_strip(self.pending)
        ):
            self.pending = vstack(self.pending, im)
            self.pending_name = f"{self.pending_name}+{name}"
            self.stats["strips_glued"] += 1
            return
        self.flush()
        self.pending, self.pending_name = im, name

    def flush(self):
        if self.pending is not None:
            place_image(self.writer, self.pending, self.pending_name, self.stats)
            self.pending = None
            self.pending_name = ""


def merge_chunks(qpdf_exe: str, chunk_files: list[Path], output_file: Path):
    if not chunk_files:
        raise RuntimeError("No chunk PDFs were created.")

    cmd = [qpdf_exe, "--empty", "--pages"]

    for chunk in chunk_files:
        cmd.append(str(chunk))
        cmd.append("1-z")

    # Merge into a ".part" file and rename it into place only on success, so
    # an interrupted run (Ctrl+C, closed window, power loss) can never leave a
    # truncated PDF that the next run would skip as "already done".
    part_file = output_file.with_name(output_file.name + ".part")
    cmd.extend(["--", str(part_file)])

    try:
        # qpdf exits 3 when it succeeded but printed warnings (common with
        # slightly odd input); the PDF is written, so only other codes fail.
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if result.returncode not in (0, 3) or not part_file.exists():
            raise subprocess.CalledProcessError(result.returncode, cmd)
        os.replace(part_file, output_file)
    finally:
        try:
            part_file.unlink()
        except OSError:
            pass


def process_book(source_name: str, book_folder: Path, output_file: Path, qpdf_exe: str):
    """Convert a single book folder into one Apple Books PDF (worker process)."""
    label = f"{source_name} / {book_folder.name}"
    start = time.perf_counter()
    stats = {"unreadable": [], "tiny_skipped": [], "strips_glued": 0}

    if output_file.exists() and SKIP_EXISTING_OUTPUT:
        return ("skipped", label, 0, 0.0, stats)

    images = collect_images(book_folder)

    if not images:
        return ("empty", label, 0, 0.0, stats)

    warn_duplicate_chapters(book_folder)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists():
        output_file.unlink()

    try:
        print(f"  START  {label}  ({len(images)} images)", flush=True)
        with tempfile.TemporaryDirectory() as temp:
            chunk_dir = Path(temp) / "chunks"
            writer = ChunkedPDFWriter(chunk_dir)

            total = len(images)
            gluer = StripGluer(writer, stats)
            for index, image_path in enumerate(images, start=1):
                im = load_image(image_path, stats)
                if im is not None:
                    gluer.push(im, image_path.name)

                if index % 50 == 0:
                    gc.collect()
                if index % PROGRESS_EVERY == 0 or index == total:
                    print(f"   ....  {label}: {index}/{total} images, "
                          f"{writer.total_page_count} pages", flush=True)

            gluer.flush()
            writer.close()

            pages = writer.total_page_count
            if pages == 0:
                # Every image was skipped or unreadable: a zero-page chunk
                # would make qpdf fail, so report it as "no images" instead.
                print(f"  EMPTY  {label}  (no usable pages)", flush=True)
                return ("empty", label, 0, 0.0, stats)
            print(f"  MERGE  {label}  (qpdf merging {pages} pages)...", flush=True)
            merge_chunks(qpdf_exe, writer.chunk_files, output_file)

        secs = time.perf_counter() - start
        return ("done", label, pages, secs, stats)

    except Exception as e:
        print(f"ERROR while processing {label}: {e}")
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
        return ("failed", label, 0, time.perf_counter() - start, stats)


def _run_book(marker: str, source_name: str, book_folder: Path, output_file: Path, qpdf_exe: str):
    """Worker entry point: process_book, with a marker file that only survives
    if the worker process dies mid-book (so a crash can be pinned on a book)."""
    Path(marker).touch()
    try:
        return process_book(source_name, book_folder, output_file, qpdf_exe)
    finally:
        try:
            os.unlink(marker)
        except OSError:
            pass


def discover_source_folders() -> list[Path]:
    if SOURCE_NAMES:
        sources = []
        for name in SOURCE_NAMES:
            folder = MANGA_ROOT / name
            if folder.is_dir():
                sources.append(folder)
            else:
                print(f"WARNING: configured source folder not found: {folder}")
        return sources

    sources = []
    for entry in sorted(MANGA_ROOT.iterdir(), key=natural_key):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.name in IGNORED_TOP_LEVEL:
            continue
        sources.append(entry)
    return sources


def discover_books(source_folder: Path) -> list[Path]:
    books = []
    for entry in sorted(source_folder.iterdir(), key=natural_key):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        books.append(entry)
    return books


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(
        description="Convert folders of images into Apple Books-friendly PDFs (one per book)."
    )
    parser.add_argument(
        "-o", "--output", metavar="FOLDER",
        help="folder to write the PDFs into (default: _Chunked_Apple_Books_PDFs next to "
             "the sources, or $MANGA_OUTPUT if set)",
    )
    return parser.parse_args()


def main():
    global OUTPUT_ROOT
    args = parse_args()
    if args.output:
        OUTPUT_ROOT = Path(args.output).expanduser().resolve()

    qpdf_exe = find_qpdf()
    if qpdf_exe is None:
        print("ERROR: qpdf not found. It is needed to merge the chunk PDFs, so nothing was converted.")
        if sys.platform == "darwin":
            print("  Install it:  brew install qpdf")
        else:
            print("  Install it:  winget install -e --id QPDF.QPDF")
            print("  (then close this window and run the launcher again so the new PATH is picked up)")
        sys.exit(1)

    if not MANGA_ROOT.exists():
        print("ERROR: Manga root does not exist:")
        print(MANGA_ROOT)
        sys.exit(1)

    source_folders = discover_source_folders()
    if not source_folders:
        print(f"ERROR: No source folders found under {MANGA_ROOT}")
        sys.exit(1)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    # Leftovers from an interrupted merge (never valid PDFs).
    for stale in OUTPUT_ROOT.glob("*.pdf.part"):
        try:
            stale.unlink()
            print(f"Removed stale partial file: {stale.name}")
        except OSError:
            pass

    jobs: list[tuple[str, Path, Path]] = []
    used_names: set[str] = set()
    for source_folder in source_folders:
        for book_folder in discover_books(source_folder):
            filename = safe_filename(book_folder.name) + ".pdf"
            if filename.lower() in used_names:
                filename = safe_filename(
                    f"{book_folder.name} ({source_folder.name})"
                ) + ".pdf"
            counter = 2
            while filename.lower() in used_names:
                filename = safe_filename(f"{book_folder.name} ({counter})") + ".pdf"
                counter += 1
            used_names.add(filename.lower())  # Windows/macOS names are case-insensitive
            jobs.append((source_folder.name, book_folder, OUTPUT_ROOT / filename))

    if not jobs:
        print("No books found to process.")
        return

    # Pre-filter already-done books so we don't spawn a worker just to skip.
    if SKIP_EXISTING_OUTPUT:
        pending = [j for j in jobs if not j[2].exists()]
        skipped_existing = len(jobs) - len(pending)
    else:
        pending, skipped_existing = jobs, 0

    avail = available_ram_gb()
    workers = min(decide_workers(), max(1, len(pending)))

    print("#" * 80)
    print(f"Batch: {len(jobs)} book(s) across {len(source_folders)} source folder(s)")
    ram_kind = "total" if sys.platform == "darwin" else "free"
    ram_note = f"  (~{avail:.0f}GB RAM {ram_kind}, ~{RAM_PER_WORKER_GB:.1f}GB/worker budget)" if avail else ""
    print(f"To convert: {len(pending)}   already done (skipped): {skipped_existing}")
    print(f"Parallel workers: {workers}{ram_note}   (cores: {os.cpu_count()})")
    print(f"Output folder: {OUTPUT_ROOT}")
    for source_name, book_folder, output_file in pending:
        print(f"  - {source_name} / {book_folder.name}  ->  {output_file.name}")
    print("#" * 80, flush=True)

    if not pending:
        print("Nothing to do. Clear the output folder for a full rebuild.")
        return

    results = {"done": [], "skipped": [], "empty": [], "failed": []}
    book_stats: dict[str, dict] = {}
    batch_start = time.perf_counter()

    set_keep_awake(True)
    marker_dir = Path(tempfile.mkdtemp(prefix="manga_pdf_"))
    try:
        ctx = mp.get_context("spawn")  # Windows has no fork
        pool_kwargs = {"mp_context": ctx}
        if TASKS_PER_WORKER and sys.version_info >= (3, 11):
            pool_kwargs["max_tasks_per_child"] = TASKS_PER_WORKER

        # A worker that dies outright (out of memory, a crash inside an image
        # decoder) breaks the whole pool. When that happens the pool is rebuilt
        # with one worker fewer and the unfinished books are resubmitted. Books
        # that were mid-conversion are retried on their own first, so a book
        # that crashes a second time is identified exactly and marked failed.
        queue = list(enumerate(pending))
        crashes: dict[int, int] = {}
        pool_workers = workers
        done_count = 0
        total = len(pending)
        while queue:
            suspects = [j for j in queue if crashes.get(j[0])]
            batch = suspects[:1] if suspects else queue
            batch_workers = 1 if suspects else min(pool_workers, len(batch))
            unfinished = []

            with ProcessPoolExecutor(max_workers=batch_workers, **pool_kwargs) as executor:
                futures = {
                    executor.submit(_run_book, str(marker_dir / f"{i}.started"), s, b, o, qpdf_exe): (i, (s, b, o))
                    for (i, (s, b, o)) in batch
                }
                for future in as_completed(futures):
                    job = futures[future]
                    s, b, o = job[1]
                    try:
                        status, label, pages, secs, stats = future.result()
                    except BrokenProcessPool:
                        unfinished.append(job)
                        continue
                    except Exception as e:
                        done_count += 1
                        label = f"{s} / {b.name}"
                        print(f"[{done_count}/{total}] !! crashed: {label}  ({e})")
                        results["failed"].append(label)
                        continue

                    done_count += 1
                    results[status].append(label)
                    book_stats[label] = stats

                    if status == "done":
                        print(f"[{done_count}/{total}] OK   {label}  -  {pages} pages in {secs:.1f}s", flush=True)
                    elif status == "skipped":
                        print(f"[{done_count}/{total}] skip {label}  (output already exists)", flush=True)
                    elif status == "empty":
                        print(f"[{done_count}/{total}] --   {label}  (no images)", flush=True)
                    else:
                        print(f"[{done_count}/{total}] FAIL {label}", flush=True)

            done_ids = {j[0] for j in batch} - {j[0] for j in unfinished}
            queue = [j for j in queue if j[0] not in done_ids]
            if not unfinished:
                continue

            # Which unfinished books were actually being converted when it broke?
            running = [j for j in unfinished if (marker_dir / f"{j[0]}.started").exists()]
            if not running:
                running = unfinished  # cannot tell: count it against all of them
            for j in running:
                (marker_dir / f"{j[0]}.started").unlink(missing_ok=True)
                crashes[j[0]] = crashes.get(j[0], 0) + 1
            pool_workers = max(1, pool_workers - 1)
            names = ", ".join(f"{j[1][0]} / {j[1][1].name}" for j in running)
            print(f"!! A worker process crashed (while converting: {names}). "
                  f"Restarting with {pool_workers} worker(s) and retrying unfinished books.", flush=True)
            for j in running:
                if crashes[j[0]] >= 2:
                    done_count += 1
                    label = f"{j[1][0]} / {j[1][1].name}"
                    print(f"[{done_count}/{total}] FAIL {label}  (crashed the converter twice)", flush=True)
                    results["failed"].append(label)
                    o = j[1][2]
                    try:
                        o.with_name(o.name + ".part").unlink()
                    except OSError:
                        pass
                    queue = [q for q in queue if q[0] != j[0]]
    finally:
        set_keep_awake(False)
        shutil.rmtree(marker_dir, ignore_errors=True)

    elapsed = time.perf_counter() - batch_start

    print()
    print("#" * 80)
    print("BATCH COMPLETE")
    print("#" * 80)
    print(f"Total wall time: {elapsed:.1f}s")
    print(f"Converted: {len(results['done'])}")
    print(f"Skipped (no images): {len(results['empty'])}")
    print(f"Already existed (not redone): {skipped_existing}")
    print(f"Failed: {len(results['failed'])}")

    if results["failed"]:
        print()
        print("The following books failed and were not written:")
        for item in results["failed"]:
            print(f"  - {item}")

    glued = sum(st["strips_glued"] for st in book_stats.values())
    tiny = sum(len(st["tiny_skipped"]) for st in book_stats.values())
    if glued or tiny:
        print()
        print(f"Thin strips glued onto neighbouring pages: {glued}   "
              f"tiny images skipped (no same-width neighbour): {tiny}")

    unreadable = [(label, st["unreadable"]) for label, st in book_stats.items() if st["unreadable"]]
    if unreadable:
        print()
        print("!" * 80)
        print("UNREADABLE FILES - these are not valid images (corrupt or garbled downloads).")
        print("The PDFs were written WITHOUT these pages. Re-download the chapters listed:")
        for label, files in unreadable:
            chapters = sorted({Path(f).parent.name for f in files})
            print(f"  - {label}: {len(files)} file(s) in {', '.join(chapters)}")
            for f in files:
                print(f"        {Path(f).parent.name}/{Path(f).name}")
        print("!" * 80)

    print()
    print(f"Output folder: {OUTPUT_ROOT}")

    if results["failed"]:
        sys.exit(1)  # the launchers then report "Finished with errors"


if __name__ == "__main__":
    mp.freeze_support()  # required for the spawn start method on Windows
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(1)
