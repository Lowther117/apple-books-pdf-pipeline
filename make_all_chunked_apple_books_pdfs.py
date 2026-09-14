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

from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


# ----------------------------------------------------------------------------
# Batch configuration  (identical to the original unless noted)
# ----------------------------------------------------------------------------
# Root that contains the per-source folders. On Windows this defaults to the
# folder this script lives in (the .bat does not set MANGA_ROOT).
MANGA_ROOT = Path(os.environ.get("MANGA_ROOT", str(Path(__file__).resolve().parent)))

# Leave empty to auto-detect every top-level folder under MANGA_ROOT (ignoring
# hidden, ".venv", and "_..." folders). Each immediate subfolder of a source
# folder is one book -> one PDF.
SOURCE_NAMES: list[str] = []  # e.g. ["MangaDex", "MangaRead", "ManhuaPlus"]

# Where the PDFs go. Default: "_Chunked_Apple_Books_PDFs" next to the sources.
# Override with the MANGA_OUTPUT env var (the launchers pass through a folder
# dropped on / given to them) or with --output / -o on the command line.
OUTPUT_ROOT = Path(os.environ.get("MANGA_OUTPUT") or str(MANGA_ROOT / "_Chunked_Apple_Books_PDFs")).expanduser()

# Skip books whose output PDF already exists (resume / add-new without redoing).
SKIP_EXISTING_OUTPUT = True

# Folders never treated as a source or a book.
IGNORED_TOP_LEVEL = {".venv"}

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
    """Best-effort available physical RAM in GB (Windows via ctypes)."""
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
# Conversion core  (IDENTICAL to the Mac original)
# ----------------------------------------------------------------------------

def natural_key(path: Path):
    text = str(path).lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "-")
    return name.strip()


def is_image(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and not path.name.startswith(".")
    )


def collect_images(book_folder: Path):
    images = [
        p for p in book_folder.rglob("*")
        if is_image(p)
        and "_Apple_Books" not in str(p)
        and "_PDFs" not in str(p)
        and "_EPUBs" not in str(p)
        and "_CBZ" not in str(p)
    ]
    return sorted(images, key=natural_key)


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


def add_image(writer: ChunkedPDFWriter, image_path: Path):
    try:
        with Image.open(image_path) as im:
            im = ImageOps.exif_transpose(im)

            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            elif im.mode == "L":
                im = im.convert("RGB")

            width, height = im.size

            if width < MIN_SLICE_WIDTH_PX or height < MIN_SLICE_HEIGHT_PX:
                print(f"  WARNING: skipping tiny source image {image_path.name} ({width}x{height})")
                return

            aspect_tall = height / width
            aspect_wide = width / height

            if aspect_tall >= TALL_ASPECT_TRIGGER:
                split_tall(writer, im, image_path.name)
            elif aspect_wide >= WIDE_ASPECT_TRIGGER:
                split_wide(writer, im, image_path.name)
            else:
                writer.add_page(im)

    except Exception as e:
        print(f"  WARNING: could not process image: {image_path}")
        print(f"  Reason: {e}")


def merge_chunks(qpdf_exe: str, chunk_files: list[Path], output_file: Path):
    if not chunk_files:
        raise RuntimeError("No chunk PDFs were created.")

    cmd = [qpdf_exe, "--empty", "--pages"]

    for chunk in chunk_files:
        cmd.append(str(chunk))
        cmd.append("1-z")

    cmd.extend(["--", str(output_file)])

    subprocess.run(cmd, check=True)


def process_book(source_name: str, book_folder: Path, output_file: Path, qpdf_exe: str):
    """Convert a single book folder into one Apple Books PDF (worker process)."""
    label = f"{source_name} / {book_folder.name}"
    start = time.perf_counter()

    if output_file.exists() and SKIP_EXISTING_OUTPUT:
        return ("skipped", label, 0, 0.0)

    images = collect_images(book_folder)

    if not images:
        return ("empty", label, 0, 0.0)

    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists():
        output_file.unlink()

    try:
        print(f"  START  {label}  ({len(images)} images)", flush=True)
        with tempfile.TemporaryDirectory() as temp:
            chunk_dir = Path(temp) / "chunks"
            writer = ChunkedPDFWriter(chunk_dir)

            total = len(images)
            for index, image_path in enumerate(images, start=1):
                add_image(writer, image_path)

                if index % 50 == 0:
                    gc.collect()
                if index % PROGRESS_EVERY == 0 or index == total:
                    print(f"   ....  {label}: {index}/{total} images, "
                          f"{writer.total_page_count} pages", flush=True)

            writer.close()

            pages = writer.total_page_count
            if pages == 0:
                # Every image was skipped or unreadable: a zero-page chunk
                # would make qpdf fail, so report it as "no images" instead.
                print(f"  EMPTY  {label}  (no usable pages)", flush=True)
                return ("empty", label, 0, 0.0)
            print(f"  MERGE  {label}  (qpdf merging {pages} pages)...", flush=True)
            merge_chunks(qpdf_exe, writer.chunk_files, output_file)

        secs = time.perf_counter() - start
        return ("done", label, pages, secs)

    except Exception as e:
        print(f"ERROR while processing {label}: {e}")
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
        return ("failed", label, 0, time.perf_counter() - start)


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

    jobs: list[tuple[str, Path, Path]] = []
    used_names: set[str] = set()
    for source_folder in source_folders:
        for book_folder in discover_books(source_folder):
            filename = safe_filename(book_folder.name) + ".pdf"
            if filename in used_names:
                filename = safe_filename(
                    f"{book_folder.name} ({source_folder.name})"
                ) + ".pdf"
            counter = 2
            while filename in used_names:
                filename = safe_filename(f"{book_folder.name} ({counter})") + ".pdf"
                counter += 1
            used_names.add(filename)
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
    ram_note = f"  (~{avail:.0f}GB RAM free, ~{RAM_PER_WORKER_GB:.1f}GB/worker budget)" if avail else ""
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
    batch_start = time.perf_counter()

    set_keep_awake(True)
    try:
        ctx = mp.get_context("spawn")  # Windows has no fork
        pool_kwargs = {"max_workers": workers, "mp_context": ctx}
        if TASKS_PER_WORKER and sys.version_info >= (3, 11):
            pool_kwargs["max_tasks_per_child"] = TASKS_PER_WORKER

        with ProcessPoolExecutor(**pool_kwargs) as executor:
            futures = {
                executor.submit(process_book, s, b, o, qpdf_exe): f"{s} / {b.name}"
                for (s, b, o) in pending
            }

            done_count = 0
            total = len(futures)
            for future in as_completed(futures):
                done_count += 1
                try:
                    status, label, pages, secs = future.result()
                except Exception as e:
                    label = futures[future]
                    print(f"[{done_count}/{total}] !! crashed: {label}  ({e})")
                    results["failed"].append(label)
                    continue

                results[status].append(label)

                if status == "done":
                    print(f"[{done_count}/{total}] OK   {label}  -  {pages} pages in {secs:.1f}s", flush=True)
                elif status == "skipped":
                    print(f"[{done_count}/{total}] skip {label}  (output already exists)", flush=True)
                elif status == "empty":
                    print(f"[{done_count}/{total}] --   {label}  (no images)", flush=True)
                else:
                    print(f"[{done_count}/{total}] FAIL {label}", flush=True)
    finally:
        set_keep_awake(False)

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

    print()
    print(f"Output folder: {OUTPUT_ROOT}")


if __name__ == "__main__":
    mp.freeze_support()  # required for the spawn start method on Windows
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(1)
