"""OCR memory and CPU benchmark for the packaging plan (docs/plans/packaging.md).

Runs ScreenMind's OCR on saved screenshots in this process and prints memory
and CPU per frame. It grabs nothing from the screen and writes nothing to the
DB. Memory is what the OS task monitor shows: phys_footprint on macOS, private
working set (USS) on Windows.

    uv run --with psutil python packaging/ocr_mem_bench.py default
    uv run --with psutil python packaging/ocr_mem_bench.py tuned

Options: --frames N (default 20), --dir PATH (default: the newest day folder in
~/.screenmind/screenshots).
"""
import argparse
import os
import sys
import time
from pathlib import Path

import psutil


def memory_mb() -> float:
    if sys.platform == "darwin":
        import ctypes

        buf = ctypes.create_string_buffer(512)
        # RUSAGE_INFO_V4; ri_phys_footprint is the 8-byte field at offset 72.
        ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage(os.getpid(), 4, buf)
        return int.from_bytes(buf.raw[72:80], "little") / 2**20
    return psutil.Process().memory_full_info().uss / 2**20


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["default", "tuned"])
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--dir", type=Path)
    args = ap.parse_args()

    shots = args.dir or sorted((Path.home() / ".screenmind" / "screenshots").iterdir())[-1]
    files = sorted(shots.glob("*.jpg"))
    step = max(1, len(files) // args.frames)
    files = files[::step][: args.frames]
    if not files:
        sys.exit(f"No screenshots in {shots}")

    from PIL import Image

    if args.mode == "tuned":
        import onnxruntime as ort

        orig_init = ort.InferenceSession.__init__

        def init(self, path, sess_options=None, *a, **k):
            so = sess_options or ort.SessionOptions()
            so.enable_mem_pattern = False
            so.intra_op_num_threads = 2
            return orig_init(self, path, so, *a, **k)

        ort.InferenceSession.__init__ = init

    from screenmind.engine.ocr import OCRExtractor

    start = memory_mb()
    ocr = OCRExtractor()
    ocr._ensure_reader()
    loaded = memory_mb()
    proc = psutil.Process()
    cpu0 = sum(proc.cpu_times()[:2])
    t0 = time.time()
    mems = []
    for f in files:
        with Image.open(f) as img:
            img.load()
            ocr.extract_text(img)
        mems.append(memory_mb())
    wall = time.time() - t0
    cpu = sum(proc.cpu_times()[:2]) - cpu0
    print(f"{args.mode}: {len(files)} frames from {shots}")
    print(f"  memory MB: start {start:.0f}, models loaded {loaded:.0f}, after 1 frame {mems[0]:.0f}, "
          f"at end {mems[-1]:.0f}, max {max(mems):.0f}")
    print(f"  per frame: {wall / len(files):.2f} s wall, {cpu / len(files):.1f} CPU-s; "
          f"logical CPUs {psutil.cpu_count()}")


if __name__ == "__main__":
    main()
