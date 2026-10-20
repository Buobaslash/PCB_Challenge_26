from collections import defaultdict

total = 0.0
for label, group in (("mux", MUXES), ("flipflop", FLIPFLOPS), ("dual Q", DUAL_TRANSISTORS)):
    kinds = defaultdict(list)
    for ref in group:
        fp = fps.get(ref)
        if fp:
            w, h = size_mm(fp)
            key = (str(fp.GetFPID().GetLibItemName()), round(w, 1), round(h, 1))
            kinds[key].append(ref)
            total += (w + IC_GAP) * (h + IC_GAP)
    for (name, w, h), rs in kinds.items():
        print(f"{label}: {len(rs):2d} x {name}  pads {w} x {h} mm")

print(f"Area needed (pads + gap): {total:.0f} mm2, B.Cu band available: {38 * 33:.0f} mm2")