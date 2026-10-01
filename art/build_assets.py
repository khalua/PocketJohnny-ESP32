#!/usr/bin/env python3
"""Converts art/source/*.png into firmware image data for PocketJohnny.

Outputs (both committed, so a firmware build doesn't need Python/Pillow):
  src/assets/johnny.bin  -- all pixel data, embedded into flash
  src/johnny_assets.h    -- table of parts: name, offset, position, size

The base is master image scaled to the 466x466 panel as plain RGB565. Every
other PNG is a variant of the master with one feature changed; the script
cuts that feature's region (by filename prefix, see REGIONS) and stores it
as RGB565A8 with feathered edges, so it blends seamlessly over the base.

Run after adding or changing art:
    art/.venv/bin/python art/build_assets.py
(one-time setup: python3 -m venv art/.venv && art/.venv/bin/pip install pillow numpy)
"""
import glob
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

SIZE = 466
FEATHER = 5  # px of soft edge on each part

# Screen-space rectangles (x0, y0, x1, y1) per feature, measured by diffing
# the variants against master image and padding by a few px. They depend on
# the master's framing: if master image is replaced, re-measure them.
REGIONS = {
    "brows": (120, 100, 350, 168),
    "eyes": (130, 164, 340, 216),
    "mouth": (120, 262, 345, 402),
}

# Drawing order on screen is brows -> eyes -> mouth (bottom to top), set in
# buddy_ui.cpp. Eyes sit above brows because the brow region's lower edge
# overlaps the upper eyelid; the other way round, open-eye lashes from a brow
# variant show through a closed-eye blink.

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "source")
PROJECT = os.path.dirname(HERE)
BIN_PATH = os.path.join(PROJECT, "src", "assets", "johnny.bin")
HEADER_PATH = os.path.join(PROJECT, "src", "johnny_assets.h")


WORK = 1254  # working resolution for alignment (the generator's usual output size)


def open_rgb(path):
    img = Image.open(path).convert("RGB")
    if img.size != (WORK, WORK):
        img = img.resize((WORK, WORK), Image.LANCZOS)
    return img


def to_array(img):
    if img.size != (SIZE, SIZE):
        img = img.resize((SIZE, SIZE), Image.LANCZOS)
    return np.asarray(img).astype(np.float32)


def warp(img, s, tx, ty):
    """Output pixel x samples the input at s*(x-c)+c+t (c = image center)."""
    n = img.size[0]
    c = n / 2
    return img.transform((n, n), Image.AFFINE, (s, 0, c - s * c + tx, 0, s, c - s * c + ty), Image.BILINEAR)


def align_to_master(master_img, img):
    """The generator sometimes re-renders a variant a little larger/shifted
    versus the master. Fit scale + shift on the silhouette (everything outside
    the face's feature area) so the feature crop lines up with the base."""
    n = 314
    blur = ImageFilter.GaussianBlur(n / 200)
    m = np.asarray(master_img.resize((n, n), Image.LANCZOS).filter(blur)).astype(np.float32)
    v_img = img.resize((n, n), Image.LANCZOS).filter(blur)
    v_max = np.asarray(v_img).max(axis=2)
    yy, xx = np.mgrid[0:n, 0:n] / n
    feat = (xx > 0.2) & (xx < 0.8) & (yy > 0.25) & (yy < 0.8)
    mask = ((m.max(axis=2) > 40) | (v_max > 40)) & ~feat

    def cost(q):
        w = np.asarray(warp(v_img, *q)).astype(np.float32)
        return np.abs(m - w).mean(axis=2)[mask].mean()

    p = np.array([1.0, 0.0, 0.0])
    steps = np.array([0.02, n * 0.02, n * 0.02])
    best = cost(p)
    while steps[0] > 0.0005:
        improved = False
        for i in range(3):
            for sign in (-1, 1):
                q = p.copy()
                q[i] += sign * steps[i]
                c = cost(q)
                if c < best:
                    best, p, improved = c, q, True
        if not improved:
            steps /= 2
    # shift was fit at n px; apply at WORK px
    return warp(img, p[0], p[1] * WORK / n, p[2] * WORK / n), p, best


def refine_to_region(master_img, img, region):
    """Per-feature touch-up for variants the generator re-rendered rather than
    edited (the global fit can't fix those): fit scale + shift so the band just
    inside the region's edge matches the master, which is where the seam is."""
    x0, y0, x1, y1 = region
    band = 12
    m_s = master_img.resize((SIZE, SIZE), Image.LANCZOS)
    v_s = img.resize((SIZE, SIZE), Image.LANCZOS)
    blur = ImageFilter.GaussianBlur(1.5)
    m = np.asarray(m_s.filter(blur)).astype(np.float32)
    v_b = v_s.filter(blur)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    inside = (xx >= x0) & (xx < x1) & (yy >= y0) & (yy < y1)
    core = (xx >= x0 + band) & (xx < x1 - band) & (yy >= y0 + band) & (yy < y1 - band)
    ring = inside & ~core

    def cost(q):
        w = np.asarray(warp(v_b, *q)).astype(np.float32)
        return np.abs(m - w).mean(axis=2)[ring].mean()

    p = np.array([1.0, 0.0, 0.0])
    steps = np.array([0.01, 4.0, 4.0])
    best = cost(p)
    while steps[1] > 0.1:
        improved = False
        for i in range(3):
            for sign in (-1, 1):
                q = p.copy()
                q[i] += sign * steps[i]
                if abs(q[0] - 1) > 0.05 or abs(q[1]) > 24 or abs(q[2]) > 24:
                    continue
                c = cost(q)
                if c < best:
                    best, p, improved = c, q, True
        if not improved:
            steps /= 2
    return warp(v_s, *p), p

def find_master():
    for ext in ("png", "jpg", "jpeg"):
        p = os.path.join(SRC_DIR, f"master.{ext}")
        if os.path.isfile(p):
            return p
    sys.exit("art/source/master.(png|jpg) is missing")


def to_rgb565(rgb):
    """float RGB array -> little-endian RGB565 bytes (LVGL's native layout)."""
    rgb = np.clip(rgb + 0.5, 0, 255).astype(np.uint16)
    r, g, b = rgb[..., 0] >> 3, rgb[..., 1] >> 2, rgb[..., 2] >> 3
    return ((r << 11) | (g << 5) | b).astype("<u2").tobytes()


def prepare_base(rgb):
    """Pure black outside the round panel and in near-black areas, so those
    AMOLED pixels are fully off instead of faintly glowing."""
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    r = SIZE / 2.0
    outside = (xx + 0.5 - r) ** 2 + (yy + 0.5 - r) ** 2 > r * r
    near_black = rgb.max(axis=2) < 10
    out = rgb.copy()
    out[outside | near_black] = 0
    return out


def feather_mask(w, h):
    """255 inside, smooth ramp to 0 over FEATHER px at every edge."""
    def ramp(n):
        t = np.minimum(np.arange(n) + 0.5, n - np.arange(n) - 0.5) / FEATHER
        t = np.clip(t, 0, 1)
        return t * t * (3 - 2 * t)  # smoothstep
    return (np.outer(ramp(h), ramp(w)) * 255 + 0.5).astype(np.uint8)


def main():
    master_path = find_master()
    master_img = open_rgb(master_path)
    master = to_array(master_img)

    blob = bytearray()
    entries = []

    def add(name, data, x, y, w, h, has_alpha):
        while len(blob) % 4:  # keep every image 4-byte aligned for LVGL
            blob.append(0)
        entries.append((name, len(blob), x, y, w, h, has_alpha))
        blob.extend(data)

    base = prepare_base(master)
    add("base", to_rgb565(base), 0, 0, SIZE, SIZE, 0)

    variants = sorted(
        p for p in glob.glob(os.path.join(SRC_DIR, "*.png"))
        if not os.path.basename(p).startswith("master.")
    )
    for path in variants:
        name = os.path.splitext(os.path.basename(path))[0]
        feature = name.split("_", 1)[0]
        if feature not in REGIONS:
            print(f"skip {name}: unknown feature prefix '{feature}'")
            continue
        x0, y0, x1, y1 = REGIONS[feature]
        w, h = x1 - x0, y1 - y0
        img, (sc, tx, ty), fit = align_to_master(master_img, open_rgb(path))
        img, (rs, rx, ry) = refine_to_region(master_img, img, REGIONS[feature])
        img = to_array(img)

        drift = np.abs(img - master)
        # Border pixels should match the master closely; a big difference
        # means the generator moved something and the seams will show.
        border = np.concatenate([
            drift[y0, x0:x1].ravel(), drift[y1 - 1, x0:x1].ravel(),
            drift[y0:y1, x0].ravel(), drift[y0:y1, x1 - 1].ravel(),
        ]).mean()
        crop = prepare_base(img)[y0:y1, x0:x1]
        add(name, to_rgb565(crop) + feather_mask(w, h).tobytes(), x0, y0, w, h, 1)
        warn = "  <-- high edge drift, seams may show" if border > 25 else ""
        print(f"{name:20s} {feature:6s} {w}x{h} at ({x0},{y0})  fit s={sc:.3f}, local s={rs:.3f} d=({rx:.1f},{ry:.1f}) edge drift {border:4.1f}{warn}")

    os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
    with open(BIN_PATH, "wb") as f:
        f.write(blob)

    lines = [
        "// Generated by art/build_assets.py -- do not edit.",
        "#pragma once",
        "",
        "#include <stdint.h>",
        "",
        "typedef struct {",
        "    const char *name;",
        "    uint32_t offset;       // into johnny.bin",
        "    uint16_t x, y, w, h;   // screen position and size",
        "    uint8_t has_alpha;     // 1 = RGB565A8, 0 = RGB565",
        "} johnny_part_t;",
        "",
        f"#define JOHNNY_PART_COUNT {len(entries)}",
        "",
        "static const johnny_part_t kJohnnyParts[JOHNNY_PART_COUNT] = {",
    ]
    for name, off, x, y, w, h, a in entries:
        lines.append(f'    {{"{name}", {off}u, {x}, {y}, {w}, {h}, {a}}},')
    lines += ["};", ""]
    with open(HEADER_PATH, "w") as f:
        f.write("\n".join(lines))

    print(f"\n{len(entries)} images, {len(blob) / 1024:.0f} KB -> {os.path.relpath(BIN_PATH, PROJECT)}")


if __name__ == "__main__":
    main()
