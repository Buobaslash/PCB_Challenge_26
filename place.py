#!/usr/bin/env python3
"""
Placement script for a 40 mm x 40 mm 4-layer board (KiCad 9/10, pcbnew API).
Run in the Scripting Console:
  exec(open(r"E:\\Personal Projects\\PCB Challenge\\PCB-Challenge-2026\\my_solution\\place.py", encoding="utf-8").read())
"""
import math
import os
import re
import sys

import pcbnew
from pcbnew import FromMM, ToMM, VECTOR2I

# ----------------------------------------------------------------- settings
ORIGIN_MM = (0.0, 0.0)          # overwritten from the Edge.Cuts outline if found
BOARD_W, BOARD_H = 40.0, 40.0
EDGE_KEEPOUT = 1.0
IC_GAP = 1.0                    # pad-to-pad gap between neighbouring ICs
TOL = 0.02                      # absorbs nanometre rounding in KiCad coordinates
PART_MARGIN = 0.3               # clearance for caps and resistors
GRID_Y_MIN, GRID_Y_MAX = 8.0, 34.0

def refs(prefix, *ranges):
    out = []
    for r in ranges:
        a, b = (r, r) if isinstance(r, int) else r
        out += [f"{prefix}{i}" for i in range(a, b + 1)]
    return out

MUXES = refs("U", (10, 16), (29, 32), (35, 44), 64, (70, 73))
FLIPFLOPS = refs("U", 1, (4, 8), 17, 19, 20, 33, 34, 37, 38, (45, 50), (65, 69))
DUAL_TRANSISTORS = refs("Q", (15, 29))
DECAP_REFS = ["C13", "C14", "C15", "C20"]

FIXED_FRONT = {
    "U25": (20, 7), "J3": (20, 38), "U9": (14, 34),
    "U22": (13, 20), "U28": (27, 20), "J4": (3, 20), "J5": (37, 20),
}
THROUGH_HOLE_REFS = {"J3", "J4", "J5"}
CAP_ANCHORS = {"C13": (14, 34), "C14": (14, 34), "C15": (17, 36), "C20": (17, 32)}
POWER_NET_RE = re.compile(r"GND|VCC|VDD|VBUS|3V3|5V|^\+|^/\+", re.I)

# ------------------------------------------------------------------- helpers
def natural_key(ref):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]

def mm(x, y):
    return VECTOR2I(FromMM(ORIGIN_MM[0] + x), FromMM(ORIGIN_MM[1] + y))

def bbox_mm(fp):
    """Pad-extent box (l, t, r, b) in board-relative mm."""
    l = t = r = b = None
    for pad in fp.Pads():
        bb = pad.GetBoundingBox()
        pl, pt, pr, pb = bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()
        l = pl if l is None else min(l, pl)
        t = pt if t is None else min(t, pt)
        r = pr if r is None else max(r, pr)
        b = pb if b is None else max(b, pb)
    if l is None:
        bb = None
        for args in ((False, False), (False,), ()):
            try:
                bb = fp.GetBoundingBox(*args)
                break
            except TypeError:
                continue
        l, t, r, b = bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()
    return (ToMM(l) - ORIGIN_MM[0], ToMM(t) - ORIGIN_MM[1],
            ToMM(r) - ORIGIN_MM[0], ToMM(b) - ORIGIN_MM[1])

def size_mm(fp):
    l, t, r, b = bbox_mm(fp)
    return r - l, b - t

def set_side(fp, want_back):
    if (fp.GetLayer() == pcbnew.B_Cu) == want_back:
        return
    target = pcbnew.B_Cu if want_back else pcbnew.F_Cu
    try:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    except (TypeError, AttributeError):
        try:
            fp.SetLayerAndFlip(target)
        except AttributeError:
            fp.Flip(fp.GetPosition(), True)

def place(fp, x, y, back=False, rot=0.0):
    set_side(fp, back)
    fp.SetOrientationDegrees(rot)
    fp.SetPosition(mm(x, y))

def move_center(fp, cx, cy):
    l, t, r, b = bbox_mm(fp)
    pos = fp.GetPosition()
    fp.SetPosition(VECTOR2I(pos.x + FromMM(cx - (l + r) / 2),
                            pos.y + FromMM(cy - (t + b) / 2)))

def overlaps(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

# --------------------------------------------------------------------- setup
if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".kicad_pcb"):
    BOARD_PATH = os.path.abspath(sys.argv[1])
    board = pcbnew.LoadBoard(BOARD_PATH)
else:
    BOARD_PATH = None
    board = pcbnew.GetBoard()
if board is None:
    sys.exit("No board available.")

ob = board.GetBoardEdgesBoundingBox()
if ob.GetWidth() > 0:
    ORIGIN_MM = (ToMM(ob.GetLeft()), ToMM(ob.GetTop()))
    ow, oh = ToMM(ob.GetWidth()), ToMM(ob.GetHeight())
    print(f"Outline found: {ow:.1f} x {oh:.1f} mm, origin {ORIGIN_MM}")
    if abs(ow - BOARD_W) > 0.5 or abs(oh - BOARD_H) > 0.5:
        print("WARNING: outline size differs from BOARD_W/BOARD_H")
else:
    print("No Edge.Cuts outline found, using ORIGIN_MM as set")

fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
occupied = {"F": [], "B": []}
back_parts = {}
missing = []

def get(ref):
    fp = fps.get(ref)
    if fp is None:
        missing.append(ref)
    return fp

# ------------------------------------------------- 1. fixed F.Cu placements
for ref, (x, y) in FIXED_FRONT.items():
    fp = get(ref)
    if not fp:
        continue
    place(fp, x, y, back=False)
    rect = bbox_mm(fp)
    occupied["F"].append(rect)
    if ref in THROUGH_HOLE_REFS:
        occupied["B"].append(rect)

# --------------------------------------------------- 2. B.Cu placement
y_mid = (GRID_Y_MIN + GRID_Y_MAX) / 2
cx_board = BOARD_W / 2
ZONE_H = GRID_Y_MAX - GRID_Y_MIN

def part_cells(parts):
    cells = []
    for p in parts:
        p.SetOrientationDegrees(0)
        w, h = size_mm(p)
        cells.append((p, w, h))
    return cells

def strip_size(parts):
    cells = part_cells(parts)
    if not cells:
        return 0.0, 0.0
    cw = max(w for _, w, _ in cells) + IC_GAP
    ch = max(h for _, _, h in cells) + IC_GAP
    ncols = max(1, math.ceil(len(cells) * ch / ZONE_H))
    rows = math.ceil(len(cells) / ncols)
    return ncols * cw - IC_GAP, rows * ch - IC_GAP

def pack_region(parts, x0, y0, x1, y1):
    """Shelf-pack onto B.Cu at each part's own size. Returns parts that did not fit."""
    cells = sorted(part_cells(parts),
                   key=lambda c: (-round(c[2], 1), -round(c[1], 1),
                                  natural_key(c[0].GetReference())))
    overflow = []
    x, y, row_h = x0, y0, 0.0
    for p, w, h in cells:
        fits = False
        for _ in range(500):                              # safety counter
            if x + w > x1 + TOL and x > x0 + TOL:         # wrap to next row
                y += row_h if row_h > 0 else 1.0
                x, row_h = x0, 0.0
                continue
            if y + h > y1 + TOL:
                break
            probe = (x - IC_GAP + TOL, y - IC_GAP + TOL,
                     x + w + IC_GAP - TOL, y + h + IC_GAP - TOL)
            hit = next((o for o in occupied["B"] if overlaps(probe, o)), None)
            if hit:
                x = hit[2] + IC_GAP
                continue
            fits = True
            break
        if not fits:
            overflow.append(p)
            continue
        cx, cy = x + w / 2, y + h / 2
        set_side(p, True)
        p.SetOrientationDegrees(0)
        p.SetPosition(mm(cx, cy))
        move_center(p, cx, cy)
        occupied["B"].append(bbox_mm(p))
        back_parts[p.GetReference()] = p
        x += w + IC_GAP
        row_h = max(row_h, h + IC_GAP)
    return overflow

def edge_limits():
    lo, hi = EDGE_KEEPOUT + 1.0, BOARD_W - EDGE_KEEPOUT - 1.0
    j4, j5 = fps.get("J4"), fps.get("J5")
    if j4:
        lo = max(lo, bbox_mm(j4)[2] + IC_GAP + 0.1)
    if j5:
        hi = min(hi, bbox_mm(j5)[0] - IC_GAP - 0.1)
    return lo, hi

OFFSETS = sorted(((dx * 0.5, dy * 0.5) for dx in range(-60, 61) for dy in range(-60, 61)),
                 key=lambda o: o[0] ** 2 + o[1] ** 2)

def place_near(fp, anchor, back):
    set_side(fp, back)
    fp.SetOrientationDegrees(0)
    w, h = size_mm(fp)
    occ = occupied["B" if back else "F"]
    for dx, dy in OFFSETS:
        cx, cy = anchor[0] + dx, anchor[1] + dy
        rect = (cx - w / 2 - PART_MARGIN, cy - h / 2 - PART_MARGIN,
                cx + w / 2 + PART_MARGIN, cy + h / 2 + PART_MARGIN)
        if (rect[0] < EDGE_KEEPOUT or rect[1] < EDGE_KEEPOUT or
                rect[2] > BOARD_W - EDGE_KEEPOUT or rect[3] > BOARD_H - EDGE_KEEPOUT):
            continue
        if any(overlaps(rect, o) for o in occ):
            continue
        fp.SetPosition(mm(cx, cy))
        move_center(fp, cx, cy)
        occ.append(bbox_mm(fp))
        return True
    return False

def present(refs_):
    return [p for p in (get(r) for r in sorted(refs_, key=natural_key)) if p]

mux, ffs, qs = present(MUXES), present(FLIPFLOPS), present(DUAL_TRANSISTORS)
ff_l, ff_r = ffs[:math.ceil(len(ffs) / 2)], ffs[math.ceil(len(ffs) / 2):]
q_l, q_r = qs[:math.ceil(len(qs) / 2)], qs[math.ceil(len(qs) / 2):]
lo, hi = edge_limits()
overflow = []

# Left, from the J4 edge inward: dual transistors, then flip-flops
wq, hq = strip_size(q_l)
overflow += pack_region(q_l, lo, 20.0 - hq / 2, lo + wq, 20.0 + hq / 2)
x = lo + wq + (IC_GAP if wq else 0)
wf, hf = strip_size(ff_l)
overflow += pack_region(ff_l, x, y_mid - hf / 2, x + wf, y_mid + hf / 2)
mux_x0 = x + wf + IC_GAP

# Right, from the J5 edge inward
wq, hq = strip_size(q_r)
overflow += pack_region(q_r, hi - wq, 20.0 - hq / 2, hi, 20.0 + hq / 2)
x = hi - wq - (IC_GAP if wq else 0)
wf, hf = strip_size(ff_r)
overflow += pack_region(ff_r, x - wf, y_mid - hf / 2, x, y_mid + hf / 2)
mux_x1 = x - wf - IC_GAP

print(f"Mux region: {mux_x1 - mux_x0:.1f} mm wide x {ZONE_H:.1f} mm tall")
overflow += pack_region(mux, mux_x0, GRID_Y_MIN, mux_x1, GRID_Y_MAX)

for p in overflow:
    if place_near(p, (cx_board, y_mid), back=False):
        print(f"{p.GetReference()}: did not fit on B.Cu, placed on F.Cu")
    else:
        print(f"WARNING: no free spot for {p.GetReference()}")

# ---------------------------------------- 3. caps and resistors
for ref in DECAP_REFS:
    fp = get(ref)
    if fp and not place_near(fp, CAP_ANCHORS[ref], back=False):
        print(f"WARNING: no free spot found for {ref}")

net_members = {}
for fp in board.GetFootprints():
    for pad in fp.Pads():
        name = pad.GetNetname()
        if name:
            net_members.setdefault(name, set()).add(fp.GetReference())

def parent_anchor(rref):
    scores = {}
    for name, members in net_members.items():
        if rref not in members or POWER_NET_RE.search(name) or len(members) > 8:
            continue
        for m in members:
            if m in back_parts:
                scores[m] = scores.get(m, 0) + (2 if m.startswith("Q") else 1)
    if not scores:
        return (cx_board, y_mid)
    best = max(scores, key=scores.get)
    l, t, r, b = bbox_mm(back_parts[best])
    return ((l + r) / 2, (t + b) / 2)

resistors = sorted((r for r in fps if re.fullmatch(r"R\d+", r)), key=natural_key)
for rref in resistors:
    anchor = parent_anchor(rref)
    if place_near(fps[rref], anchor, back=True):
        continue
    if place_near(fps[rref], anchor, back=False):
        print(f"{rref}: B.Cu full near its parent, placed on F.Cu")
    else:
        print(f"WARNING: no free spot found for {rref} on either side")
# ------------------------------------- 4. everything the script did not handle
SKIP_PREFIXES = ("H", "MH", "FID", "LOGO", "REF")   # left where they are

handled = (set(FIXED_FRONT) | set(MUXES) | set(FLIPFLOPS) |
           set(DUAL_TRANSISTORS) | set(DECAP_REFS) | set(resistors))
leftovers = sorted(set(fps) - handled, key=natural_key)

by_prefix = {}
for r in leftovers:
    by_prefix.setdefault(re.match(r"[A-Za-z]+", r).group(0) if re.match(r"[A-Za-z]+", r) else "?", []).append(r)
print("Unhandled footprints:")
for pre, lst in sorted(by_prefix.items()):
    print(f"  {pre}: {len(lst)}  ({', '.join(lst)})")

placed_refs = (handled - set(missing)) & set(fps)
GND_RE = re.compile(r"GND|VSS", re.I)

def anchor_any(ref):
    scores = {}
    for name, members in net_members.items():
        if ref not in members or GND_RE.search(name) or len(members) > 12:
            continue
        for m in members:
            if m != ref and m in placed_refs:
                scores[m] = scores.get(m, 0.0) + 1.0 / len(members)
    if not scores:
        return (cx_board, y_mid), False
    best = max(scores, key=scores.get)
    l, t, r, b = bbox_mm(fps[best])
    return ((l + r) / 2, (t + b) / 2), best in back_parts

for ref in leftovers:
    pre = re.match(r"[A-Za-z]+", ref)
    if pre and pre.group(0).upper() in SKIP_PREFIXES:
        print(f"{ref}: left in place")
        continue
    fp = fps[ref]
    anchor, parent_on_back = anchor_any(ref)
    if fp.HasThroughHolePads():
        if place_near(fp, anchor, back=False):
            occupied["B"].append(bbox_mm(fp))      # block both sides
            placed_refs.add(ref)
        else:
            print(f"WARNING: no free spot for {ref}")
        continue
    done = False
    for side in (parent_on_back, not parent_on_back):
        if place_near(fp, anchor, back=side):
            if side:
                back_parts[ref] = fp
            placed_refs.add(ref)
            done = True
            break
    if not done:
        print(f"WARNING: no free spot for {ref} on either side")
# ------------------------------------------------------------------- finish
if missing:
    print("Footprints not found on board:", ", ".join(sorted(set(missing), key=natural_key)))
try:
    pcbnew.Refresh()
except Exception:
    pass
if BOARD_PATH:
    out = BOARD_PATH.replace(".kicad_pcb", "_placed.kicad_pcb")
    pcbnew.SaveBoard(out, board)
    print("Saved", out)