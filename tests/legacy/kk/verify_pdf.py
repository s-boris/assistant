#!/usr/local/bin/python3.13 -I
"""Cross-check kk-compare's CSV parsing against the BAG premium guide PDF (Wegweiser) for one canton.

Parses every table of the given age group (default adults) in `pdftotext -layout` output, maps each
row to (region, BAG type, insurer, tariff, accident, franchise) and compares it with the rows that
kk-compare loads from the BAG CSV. The PDF rounds to 0.1, so |csv - pdf| <= 0.05 counts as a match.

  verify_pdf.py PDF CANTON [--age adult|young] [-v]
Exit 0 = every PDF value matches and every CSV row of that age group appears in the PDF.
"""

import argparse
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys

KK = os.environ.get("KK_BIN") or os.path.expanduser("~/bin/kk-compare")
spec = importlib.util.spec_from_loader("kk", importlib.machinery.SourceFileLoader("kk", KK))
kk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kk)

SECTION = re.compile(r"Prämien – (Grundversicherung|FLEX|PRAXIS|TEL_DIG|PHARM)\b")
AGES = {"adult": "Erwachsene / Adultes", "young": "Junge Erwachsene / Jeunes adultes", "child": "Kinder / Enfants"}
NUM = re.compile(r"\d+\.\d+")


def parse(text, age):
    want = AGES[age]
    ttype, cur_age, cols, region = None, None, None, None
    out = []
    for line in text.splitlines():
        m = SECTION.search(line)
        if m:
            ttype = "BASE" if m.group(1) == "Grundversicherung" else m.group(1)
            continue
        if "Franchise / Franchise" in line:
            if "Junge Erwachsene" in line:
                cur_age = AGES["young"]
            elif "Erwachsene / Adultes" in line:
                cur_age = AGES["adult"]
            elif "Kinder / Enfants" in line:
                cur_age = AGES["child"]
            cols = None
            continue
        rm = re.search(r"Region / Région (\d)", line)
        if rm:
            region = int(rm.group(1))
            toks = [(t.end(), int(t.group())) for t in re.finditer(r"\b\d{1,4}\b", line[: rm.start()])]
            toks += [(t.end() + rm.end(), int(t.group())) for t in re.finditer(r"\b\d{1,4}\b", line[rm.end() :])]
            n = len(toks) // 2
            cols = (toks[:n], toks[n:], rm.start(), rm.end())
            continue
        if cur_age != want or not cols or not ttype:
            continue
        nums = list(NUM.finditer(line))
        if not nums:
            continue
        # name = the text between the left and right number blocks
        left = [t for t in nums if t.end() <= cols[2] + 2]
        right = [t for t in nums if t.start() >= cols[3] - 2]
        mid_start = left[-1].end() if left else 0
        mid_end = right[0].start() if right else len(line)
        name = line[mid_start:mid_end].strip()
        if not name or not re.search(r"[A-Za-zÀ-ÿ]", name):
            continue
        for side, toks, acc in ((cols[0], left, "MIT_UNF"), (cols[1], right, "OHN_UNF")):
            for t in toks:
                fr = min(side, key=lambda c: abs(c[0] - t.end()))[1]
                out.append((region, ttype, name, acc, fr, float(t.group())))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("canton")
    ap.add_argument("--age", default="adult", choices=["adult", "young"])
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    text = subprocess.run(["pdftotext", "-layout", a.pdf, "-"], capture_output=True, text=True, check=True).stdout
    pdf = parse(text, a.age)
    if not pdf:
        sys.exit("no PDF rows parsed")
    short = {kk.fold(v[0]): n for n, v in kk.INSURERS.items()}  # noqa: F841
    offers, years = kk.load_offers(a.canton)
    aka, sub = kk.AGE[a.age]
    csvrows = {}
    for o in offers:
        if o.age == aka and o.sub == sub:
            nm = kk.INSURERS[o.ins][0] if o.ttype == "BASE" else f"{kk.INSURERS[o.ins][0]}, {o.name}"
            csvrows[(o.region, o.ttype, kk.fold(nm), o.acc, o.fr)] = o.prem
    seen, bad, unknown = set(), [], []
    for region, ttype, name, acc, fr, val in pdf:
        key = (region, ttype, kk.fold(name), acc, fr)
        if key not in csvrows:
            unknown.append((region, ttype, name, acc, fr, val))
            continue
        seen.add(key)
        if abs(csvrows[key] - val) > 0.05 + 1e-9:
            bad.append((region, ttype, name, acc, fr, val, csvrows[key]))
    missing = sorted(k for k in csvrows if k not in seen)
    print(
        f"PDF values parsed: {len(pdf)}; CSV rows ({a.canton}, {a.age}): {len(csvrows)}; "
        f"matched: {len(seen)}; value mismatches: {len(bad)}; PDF rows not in CSV: {len(unknown)}; "
        f"CSV rows not in PDF: {len(missing)}"
    )
    for b in bad[:20]:
        print("MISMATCH", b)
    for u in unknown[:20]:
        print("NOT IN CSV", u)
    if a.v or len(missing) <= 20:
        for m in missing[:40]:
            print("NOT IN PDF", m, csvrows[m])
    sys.exit(0 if not bad and not unknown and not missing else 1)


if __name__ == "__main__":
    main()
