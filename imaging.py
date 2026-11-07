"""Perceptual diffing.

The hard part of visual regression is not detecting difference, it is *not* detecting
things that did not change. Three defences, in order of how much noise they remove:

  1. alignment search — a page that shifts 1px because a font loaded late is not a
     regression. We search a small window of offsets and keep the best-scoring one.
  2. per-channel threshold — RGB deltas below the threshold are antialiasing, not edits.
  3. region grouping — adjacent changed pixels are merged into boxes, so the report says
     "the header, 340x60 at (12, 8)" instead of "9,431 pixels differ".

Output is deliberately image-only; HTML reporting lives in render.py.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image

MAX_SIDE = 4000          # guard against a 40k-pixel-tall page eating memory
ALIGN_WINDOW = 2         # px searched in each direction; a larger move is a layout change
ALIGN_GAIN = 0.5         # a shift must at least halve the error to be believed


@dataclass
class Region:
    x: int
    y: int
    w: int
    h: int
    pixels: int

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "pixels": self.pixels}


@dataclass
class DiffResult:
    changed_pixels: int
    total_pixels: int
    ratio: float
    regions: list[Region] = field(default_factory=list)
    offset: tuple[int, int] = (0, 0)
    size_mismatch: Optional[dict] = None
    diff_png: bytes = b""
    expected_size: tuple[int, int] = (0, 0)
    actual_size: tuple[int, int] = (0, 0)


def _load(png: bytes) -> Image.Image:
    with Image.open(io.BytesIO(png)) as img:
        img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            scale = MAX_SIDE / float(max(img.size))
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        return img.copy()


def _to_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.int16)


def per_region_report(mask: np.ndarray, *, min_area: int = 12,
                      max_regions: int = 40) -> list[Region]:
    """Group changed pixels into boxes by flood-filling the mask.

    Implemented with a coarse grid rather than per-pixel BFS: screenshots are large and a
    python-level flood fill over a million pixels is a timeout, not an algorithm. Cells are
    16px; adjacent occupied cells merge into one region.
    """
    cell = 16
    h, w = mask.shape
    gh, gw = (h + cell - 1) // cell, (w + cell - 1) // cell
    if gh * gw == 0:
        return []
    grid = np.zeros((gh, gw), dtype=np.int32)
    for y in range(gh):
        band = mask[y * cell:(y + 1) * cell]
        if band.size == 0:
            continue
        for x in range(gw):
            block = band[:, x * cell:(x + 1) * cell]
            grid[y, x] = int(block.sum())

    occupied = grid > 0
    regions: list[Region] = []
    seen = np.zeros_like(occupied, dtype=bool)
    for y in range(gh):
        for x in range(gw):
            if not occupied[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            ys: list[int] = []
            xs: list[int] = []
            while stack:
                cy, cx = stack.pop()
                ys.append(cy)
                xs.append(cx)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < gh and 0 <= nx < gw and occupied[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            y0, y1 = min(ys) * cell, min(h, (max(ys) + 1) * cell)
            x0, x1 = min(xs) * cell, min(w, (max(xs) + 1) * cell)
            pixels = int(grid[min(ys):max(ys) + 1, min(xs):max(xs) + 1].sum())
            if pixels < min_area:
                continue
            regions.append(Region(x=x0, y=y0, w=x1 - x0, h=y1 - y0, pixels=pixels))

    regions.sort(key=lambda r: -r.pixels)
    return regions[:max_regions]


def _overlap(a: np.ndarray, b: np.ndarray, dx: int, dy: int, step: int = 1
             ) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """The two regions that line up when ``b`` is moved by (dx, dy), sampled identically."""
    h, w, _ = a.shape
    rows, cols = h - abs(dy), w - abs(dx)
    if rows < 8 or cols < 8:
        return None
    ya, yb = max(0, dy), max(0, -dy)
    xa, xb = max(0, dx), max(0, -dx)
    return (a[ya:ya + rows:step, xa:xa + cols:step],
            b[yb:yb + rows:step, xb:xb + cols:step])


def _score(a: np.ndarray, b: np.ndarray, dx: int, dy: int, step: int = 1) -> Optional[float]:
    pair = _overlap(a, b, dx, dy, step)
    return None if pair is None else float(np.abs(pair[0] - pair[1]).mean())


def _best_offset(a: np.ndarray, b: np.ndarray, window: int) -> tuple[int, int]:
    """Find a small shift of ``b`` that genuinely explains the difference, else (0, 0).

    The search runs on a sparse sample for speed, then the winner has to beat "no shift" at full
    resolution by ALIGN_GAIN. Without that proof, aligning unchanged images manufactures a diff.
    """
    baseline_score = _score(a, b, 0, 0)
    if not baseline_score:              # None or exactly 0.0: nothing to explain
        return (0, 0)
    h, w, _ = a.shape
    step = max(1, min(h, w) // 120)
    best = (0, 0)
    best_score = float("inf")
    for dy in range(-window, window + 1):
        for dx in range(-window, window + 1):
            score = _score(a, b, dx, dy, step)
            if score is not None and score < best_score:
                best, best_score = (dx, dy), score
    if best == (0, 0):
        return best
    full = _score(a, b, best[0], best[1])
    if full is None or full > baseline_score * ALIGN_GAIN:
        return (0, 0)
    return best


def diff(baseline_png: bytes, actual_png: bytes, *, threshold: int = 12,
         align: bool = True) -> DiffResult:
    a_img = _load(baseline_png)
    b_img = _load(actual_png)
    expected_size, actual_size = a_img.size, b_img.size
    mismatch: Optional[dict] = None

    if a_img.size != b_img.size:
        mismatch = {"baseline": list(expected_size), "actual": list(actual_size)}
        w = max(a_img.width, b_img.width)
        h = max(a_img.height, b_img.height)
        canvas_a = Image.new("RGB", (w, h), (255, 255, 255))
        canvas_b = Image.new("RGB", (w, h), (255, 255, 255))
        canvas_a.paste(a_img, (0, 0))
        canvas_b.paste(b_img, (0, 0))
        a_img, b_img = canvas_a, canvas_b

    a = _to_array(a_img)
    b = _to_array(b_img)
    offset = (0, 0)
    if align:
        offset = _best_offset(a, b, ALIGN_WINDOW)
        dx, dy = offset
        if dx or dy:
            # Start from ``a`` so the strip uncovered by the shift is not counted as a change.
            shifted = a.copy()
            target, source = _overlap(shifted, b, dx, dy)
            target[...] = source
            b = shifted

    delta = np.abs(a - b).max(axis=2)
    mask = delta > int(threshold)
    changed = int(mask.sum())
    total = int(mask.size)
    ratio = changed / float(total) if total else 0.0

    shown_base = b.astype(np.uint8)
    overlay = shown_base.copy()
    overlay[mask] = np.array([255, 0, 90], dtype=np.uint8)
    faded = (shown_base.astype(np.float32) * 0.35 + 165).astype(np.uint8)
    shown = np.where(mask[:, :, None], overlay, faded)
    buf = io.BytesIO()
    Image.fromarray(shown).save(buf, format="PNG", optimize=True)

    return DiffResult(
        changed_pixels=changed,
        total_pixels=total,
        ratio=ratio,
        regions=per_region_report(mask),
        offset=offset,
        size_mismatch=mismatch,
        diff_png=buf.getvalue(),
        expected_size=expected_size,
        actual_size=actual_size,
    )


def compare_thumbs(png: bytes, size: int = 260) -> str:
    """A small data-URI preview for the HTML report."""
    import base64
    with Image.open(io.BytesIO(png)) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size * 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
