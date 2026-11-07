# /// script
# requires-python = ">=3.12"
# dependencies = ["imagecodecs", "numpy"]
# ///
"""Measurements behind design/proposals/czi-placed-levels.md, over HTTP range requests.

    uv run measure.py census                # the rules over conformance/zarrify/corpus_czi.txt
    uv run measure.py census --store URL    # the same over a zarrified store's tile documents
    uv run measure.py pixels URL            # do clipped pyramid tiles land where the rule says?
    uv run measure.py seams URL             # do neighboring layer-0 tiles agree where they overlap?
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random
import statistics
import struct
import time
import urllib.request

import imagecodecs
import numpy as np

CORPUS = pathlib.Path(__file__).parents[3] / "conformance/zarrify/corpus_czi.txt"
BASE_2 = [(2, 0.1, 1), (4, 0.2, 2), (8, 0.4, 3), (16, 0.8, 4), (32, 1, 5), (64, 1, 6), (128, 1, 7), (256, 2, 8), (512, 4, 9), (1024, 10, 10)]
BASE_3 = [(3, 0.1, 1), (9, 0.2, 2), (27, 0.8, 3), (81, 1.5, 4), (243, 2, 5), (729, 5, 6), (2187, 15, 7)]
LAYER_0 = (1, 0)


def fetch(url, offset=None, size=None):
    headers = {} if offset is None else {"Range": f"bytes={offset}-{offset + size - 1}"}
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                return response.read()
        except OSError:
            if attempt == 5:
                raise
            time.sleep(1 + attempt)


def directory(url):
    """The DV entries of a CZI's subblock directory (spec/zarrify/czi.md, The source)."""
    head = fetch(url, 0, 160)
    position = struct.unpack_from("<q", head, 32 + 16 + 32 + 4)[0]
    segment = fetch(url, position, 160)
    allocated = struct.unpack_from("<q", segment, 16)[0]
    count = struct.unpack_from("<i", segment, 32)[0]
    body = fetch(url, position + 160, allocated - 128)
    entries, at = [], 0
    for _ in range(count):
        file_position = struct.unpack_from("<q", body, at + 6)[0]
        dimension_count = struct.unpack_from("<i", body, at + 28)[0]
        dims = {}
        for k in range(dimension_count):
            name, start, size, _, stored = struct.unpack_from("<4siifi", body, at + 32 + 20 * k)
            dims[name.split(b"\0")[0].decode()] = (start, size, stored)
        at += 32 + 20 * dimension_count
        entries.append({
            "pos": file_position,
            "series": tuple((n, dims[n][0]) for n in "SBHIRV" if n in dims),
            "plane": tuple(dims[n][0] if n in dims else 0 for n in "TCZ"),
            "x": dims["X"][0], "y": dims["Y"][0], "w": dims["X"][1], "h": dims["Y"][1],
            "sw": dims["X"][2], "sh": dims["Y"][2],
        })
    return entries


def store_tiles(url):
    """The tile records of a zarrified store's tile arrays, until the first that is absent."""
    tiles, i = [], 0
    while True:
        try:
            document = json.loads(fetch(f"{url}/tiles/{i}/zarr.json"))
        except OSError:
            return tiles
        t = document["attributes"]["refkv_zarrified"]["czi"]
        tiles.append({
            "series": tuple(t.get("dimensions", {}).items()),
            "plane": (t["planes"]["t"], t["planes"]["c"], t["planes"]["z"]),
            "x": t["x"], "y": t["y"], "w": t["size"][0], "h": t["size"][1],
            "sw": t["stored_size"][0], "sh": t["stored_size"][1],
        })
        i += 1


def layer(t):
    """A subblock's layer (spec/zarrify/czi.md, Layers)."""
    if t["w"] == t["sw"] and t["h"] == t["sh"]:
        return LAYER_0
    f = t["w"] / t["sw"] if t["sw"] > t["sh"] else t["h"] / t["sh"]
    for kind, table in ((2, BASE_2), (3, BASE_3)):
        for v, d, n in table:
            if v - d <= f <= v + d:
                return (kind, n)
    return None


def regular(B):
    """The conditions of a regular level as the profile states them today."""
    Wl, Hl, W, H = (max(t[k] for t in B) for k in ("w", "h", "sw", "sh"))
    if Wl % W or Hl % H or Wl // W != Hl // H:
        return False
    x0, y0 = min(t["x"] for t in B), min(t["y"] for t in B)
    columns, rows, seen = collections.defaultdict(set), collections.defaultdict(set), set()
    for t in B:
        if (t["x"] - x0) % Wl or (t["y"] - y0) % Hl:
            return False
        i, j = (t["x"] - x0) // Wl, (t["y"] - y0) // Hl
        if (i, j, t["plane"]) in seen:
            return False
        seen.add((i, j, t["plane"]))
        columns[i].add((t["w"], t["sw"]))
        rows[j].add((t["h"], t["sh"]))
    last_column, last_row = max(columns), max(rows)
    return (
        all(v == {(Wl, W)} or (i == last_column and len(v) == 1) for i, v in columns.items())
        and all(v == {(Hl, H)} or (j == last_row and len(v) == 1) for j, v in rows.items())
    )


def placed(B):
    """A placed level: every tile inside one cell of an anchored lattice. (reason, details)."""
    Wl, Hl, W, H = (max(t[k] for t in B) for k in ("w", "h", "sw", "sh"))
    if Wl % W or Hl % H or Wl // W != Hl // H:
        return "factor", None
    F = Wl // W
    x0 = min(t["x"] for t in B if t["w"] == Wl)
    y0 = min(t["y"] for t in B if t["h"] == Hl)
    seen, inset = set(), 0
    for t in B:
        i, j = (t["x"] - x0) // Wl, (t["y"] - y0) // Hl
        u, v = (t["x"] - x0) % Wl, (t["y"] - y0) % Hl
        if u + t["w"] > Wl or v + t["h"] > Hl:
            return "straddles", None
        if u % F or v % F:
            return "fraction", None
        if u // F + t["sw"] > W or v // F + t["sh"] > H:
            return "overflow", None
        if (i, j, t["plane"]) in seen:
            return "shared", None
        seen.add((i, j, t["plane"]))
        inset += bool(u or v)
    columns, rows = [k[0] for k in seen], [k[1] for k in seen]
    return "ok", {"F": F, "tiles": len(B), "inset": inset, "shape": ((max(rows) - min(rows) + 1) * H, (max(columns) - min(columns) + 1) * W)}


def least_origin(starts, size, step):
    """The least x0 in [0, step) with a whole cell [x0 + i step, x0 + (i + 1) step) in every tile."""
    slack = size - step
    if slack + 1 >= step:
        return 0
    delta = [0] * (step + 1)
    starts = set(starts)
    for start in starts:
        a, e = start % step, start % step + slack + 1
        delta[a] += 1
        if e <= step:
            delta[e] -= 1
        else:
            delta[step] -= 1
            delta[0] += 1
            delta[e - step] -= 1
    covered = 0
    for origin in range(step):
        covered += delta[origin]
        if covered == len(starts):
            return origin
    return None


def largest_step(starts, size):
    """The largest step in (size / 2, size] that has an origin, so that a tile holds one cell."""
    for step in range(size, size // 2, -1):
        origin = least_origin(starts, size, step)
        if origin is not None:
            return step, origin
    return None


def cropped(B):
    """A cropped level of layer 0: one whole cell in each tile, and no cell left out. (reason, details)."""
    positions = {(t["x"], t["y"], t["w"], t["h"]) for t in B}
    sizes = {p[2:] for p in positions}
    if len(sizes) > 1:
        return "sizes", None
    W, H = sizes.pop()
    x = largest_step([p[0] for p in positions], W)
    y = largest_step([p[1] for p in positions], H)
    if not x or not y:
        return "no lattice", None
    (Tx, x0), (Ty, y0) = x, y
    cells = {}
    for p in positions:
        cell = (-((x0 - p[0]) // Tx), -((y0 - p[1]) // Ty))
        if cell in cells:
            return "shared", None
        cells[cell] = p
    columns, rows = [c[0] for c in cells], [c[1] for c in cells]
    holes = 0
    for i in range(min(columns), max(columns) + 1):
        for j in range(min(rows), max(rows) + 1):
            cx, cy = x0 + i * Tx + Tx // 2, y0 + j * Ty + Ty // 2
            if (i, j) not in cells and any(p[0] <= cx < p[0] + W and p[1] <= cy < p[1] + H for p in positions):
                holes += 1
    if holes:
        return "holes", {"holes": holes, "step": (Tx, Ty)}
    # The share of the tiles' footprint outside every cell, on a raster of 8 pixels.
    left, top = min(p[0] for p in positions), min(p[1] for p in positions)
    shape = ((max(p[1] for p in positions) + H - top) // 8 + 2, (max(p[0] for p in positions) + W - left) // 8 + 2)
    footprint, kept = np.zeros(shape, bool), np.zeros(shape, bool)
    for p in positions:
        footprint[(p[1] - top) // 8 : (p[1] + H - top) // 8, (p[0] - left) // 8 : (p[0] + W - left) // 8] = True
    for i, j in cells:
        cx, cy = x0 + i * Tx, y0 + j * Ty
        kept[(cy - top) // 8 : (cy + Ty - top) // 8, (cx - left) // 8 : (cx + Tx - left) // 8] = True
    return "ok", {
        "tiles": len(positions), "tile": (W, H), "step": (Tx, Ty),
        "shape": ((max(rows) - min(rows) + 1) * Ty, (max(columns) - min(columns) + 1) * Tx),
        "outside_cells": round(float((footprint & ~kept).sum() / footprint.sum()), 4),
    }


def layers_of(entries):
    groups = collections.defaultdict(list)
    for t in entries:
        groups[(t["series"], layer(t))].append(t)
    return groups


def report(name, entries):
    pyramid, layer_0 = collections.Counter(), collections.Counter()
    gained = inset = 0
    details = []
    for (series, l), B in sorted(layers_of(entries).items(), key=lambda kv: str(kv[0])):
        if l is None:
            continue
        if regular(B):
            (layer_0 if l == LAYER_0 else pyramid)["regular"] += 1
        elif l == LAYER_0:
            reason, d = cropped(B)
            layer_0[reason] += 1
            details.append((series, reason, d))
        else:
            reason, d = placed(B)
            pyramid[reason] += 1
            if d:
                gained += d["tiles"]
                inset += d["inset"]
    print(f"{name}: {len(entries)} subblocks; pyramid layers {dict(pyramid)} ({gained} tiles placed, {inset} inset); layer 0 {dict(layer_0)}", flush=True)
    for detail in details:
        print("   ", *detail)


def census(args):
    if args.store:
        report(args.store.rsplit("/", 1)[-1], store_tiles(args.store))
        return
    for line in CORPUS.read_text().splitlines():
        if line and not line.startswith("#"):
            url, name = line.split("|")[:2]
            report(name, directory(url))


def pixels_of(url, t):
    metadata_size, _, data_size = struct.unpack_from("<iiq", fetch(url, t["pos"] + 32, 16))
    dimension_count = struct.unpack_from("<i", fetch(url, t["pos"] + 32 + 44, 4))[0]
    header = max(256, 48 + 20 * dimension_count)
    decoded = imagecodecs.jpegxr_decode(fetch(url, t["pos"] + 32 + header + metadata_size, data_size)).astype(float)
    return decoded.mean(axis=2) if decoded.ndim == 3 else decoded


def pixels(args):
    """Minify a mosaic of layer-0 tiles under clipped factor-2 tiles, and find the best shift."""
    url = args.url
    groups = layers_of(directory(url))
    random.seed(7)
    pad, checked = 16, 0
    for (series, l), B in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if l != (2, 1) or placed(B)[0] != "ok" or checked >= 10:
            continue
        Wl, Hl = max(t["w"] for t in B), max(t["h"] for t in B)
        F = Wl // max(t["sw"] for t in B)
        x0 = min(t["x"] for t in B if t["w"] == Wl)
        y0 = min(t["y"] for t in B if t["h"] == Hl)
        inset = [t for t in B if ((t["x"] - x0) % Wl or (t["y"] - y0) % Hl) and min(t["sw"], t["sh"]) >= 200]
        random.shuffle(inset)
        for t in inset[:3]:
            mosaic = np.zeros((t["h"] + 2 * pad, t["w"] + 2 * pad))
            valid = np.zeros_like(mosaic)
            for s in groups[(series, LAYER_0)]:
                ax, ay = s["x"] - t["x"] + pad, s["y"] - t["y"] + pad
                xa, ya, xb, yb = max(0, ax), max(0, ay), min(mosaic.shape[1], ax + s["sw"]), min(mosaic.shape[0], ay + s["sh"])
                if xb > xa and yb > ya:
                    mosaic[ya:yb, xa:xb] = pixels_of(url, s)[ya - ay : yb - ay, xa - ax : xb - ax]
                    valid[ya:yb, xa:xb] = 1
            tile = pixels_of(url, t)
            errors = {}
            for dy in range(-8, 9):
                for dx in range(-8, 9):
                    window = mosaic[pad + dy : pad + dy + t["sh"] * F, pad + dx : pad + dx + t["sw"] * F]
                    seen = valid[pad + dy : pad + dy + t["sh"] * F, pad + dx : pad + dx + t["sw"] * F]
                    small = window.reshape(t["sh"], F, t["sw"], F).mean(axis=(1, 3))
                    usable = seen.reshape(t["sh"], F, t["sw"], F).min(axis=(1, 3)) > 0
                    if usable.sum() >= 1000:
                        errors[dx, dy] = ((small - tile) ** 2)[usable].mean()
            best = min(errors, key=errors.get)
            print(f"{series} {t['sw']}x{t['sh']}: variance {tile.var():.1f}, MSE at the placement {errors[0, 0]:.2f}, best MSE {errors[best]:.2f} at {best} full-resolution pixels", flush=True)
            checked += 1


def seams(args):
    """Compare neighboring layer-0 tiles in their overlap, at their recorded positions."""
    url = args.url
    random.seed(3)
    tiles = [t for t in directory(url) if layer(t) == LAYER_0]
    pairs = [
        (a, b) for a in tiles for b in tiles
        if a["series"] == b["series"] and a["x"] < b["x"] and 60 <= a["x"] + a["w"] - b["x"] <= 400
        and min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]) > 800
    ]
    random.shuffle(pairs)
    rows = []
    for a, b in pairs:
        if len(rows) == 24:
            break
        top, bottom = max(a["y"], b["y"]), min(a["y"] + a["h"], b["y"] + b["h"])
        pa = pixels_of(url, a)[top - a["y"] : bottom - a["y"], b["x"] - a["x"] : a["w"]]
        pb = pixels_of(url, b)[top - b["y"] : bottom - b["y"], : a["x"] + a["w"] - b["x"]]
        if pa.var() < 200:
            continue
        errors = {}
        for dy in range(-12, 13):
            for dx in range(-12, 13):
                ca = pa[max(0, dy) : pa.shape[0] + min(0, dy), max(0, dx) : pa.shape[1] + min(0, dx)]
                cb = pb[max(0, -dy) : pb.shape[0] + min(0, -dy), max(0, -dx) : pb.shape[1] + min(0, -dx)]
                errors[dx, dy] = ((ca - cb) ** 2).mean()
        best = min(errors, key=errors.get)
        rows.append((pa.var(), errors[0, 0], max(abs(best[0]), abs(best[1]))))
        print(f"variance {pa.var():.0f}, MSE {errors[0, 0]:.1f}, best at {best}", flush=True)
    print("median variance", statistics.median(r[0] for r in rows), "median MSE", statistics.median(r[1] for r in rows), "largest best shift", collections.Counter(r[2] for r in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    command = commands.add_parser("census")
    command.add_argument("--store")
    command.set_defaults(run=census)
    for name, run in (("pixels", pixels), ("seams", seams)):
        command = commands.add_parser(name)
        command.add_argument("url")
        command.set_defaults(run=run)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
