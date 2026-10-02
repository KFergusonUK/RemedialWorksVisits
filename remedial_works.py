#!/usr/bin/env python3
"""
Street Manager remedial works counter.

Counts permits submitted (PERMIT_SUBMITTED events, PAAs excluded as they are
not permits) and picks out those whose activity type is "Remedial works",
at any suffix.

Per promoter and overall it reports:
  * permits submitted
  * remedial permits submitted, and the % of permits that are remedial
  * how many of the remedial permits were refused or cancelled
  * re-visits: how many works had 1, 2, 3, 4+ remedial permits logged.
    Refused / cancelled remedial permits are left out of the re-visit
    counts, because a refused permit and its resubmission are one return
    to site, not two. Set COUNT_FAILED_AS_REVISIT = True to include them.

Outputs:
  <prefix>_permits.csv      one row per remedial permit submitted
  <prefix>_by_promoter.csv  the figures above for every promoter

Usage:
  python remedial_works.py 08.zip
  python remedial_works.py D:\\StreetManager\\2026        # every zip in the folder
                                                        # and its sub-folders
  python remedial_works.py path/to/folder -o fy2026_remedial

All zips found are treated as ONE dataset.
"""
import argparse, csv, json, re, sys, zipfile
from collections import defaultdict
from pathlib import Path

ACTIVITY = "remedial works"            # matched case-insensitively
SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")

COUNT_FAILED_AS_REVISIT = False
MAX_BUCKET = 4                         # last re-visit column is "4 or more"

PERMIT_FIELDS = ["work_reference_number", "permit_reference_number", "suffix",
                 "work_category", "activity_type", "promoter_organisation",
                 "highway_authority", "street_name", "town", "usrn",
                 "refused", "cancelled", "proposed_start_date", "proposed_end_date"]
BUCKETS = [f"works_with_{n}_remedial" for n in range(1, MAX_BUCKET)] + \
          [f"works_with_{MAX_BUCKET}_plus_remedials"]
PROMOTER_FIELDS = ["promoter_organisation", "permits", "remedial_permits",
                   "remedial_pct", "remedial_refused", "remedial_cancelled"] + BUCKETS


def iter_zip(path: Path):
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".json")]
            print(f"  {path}  ({len(names):,} files)", flush=True)
            for n in names:
                yield n, z.read(n)
    except zipfile.BadZipFile:
        print(f"  {path}  SKIPPED - not a valid zip", flush=True)


def iter_json(src: Path):
    """A single zip, or a folder searched recursively for zips / loose json."""
    if src.is_file():
        yield from iter_zip(src)
    elif src.is_dir():
        zips = sorted(p for p in src.rglob("*") if p.suffix.lower() == ".zip")
        loose = sorted(p for p in src.rglob("*") if p.suffix.lower() == ".json")
        if not zips and not loose:
            sys.exit(f"No .zip or .json files found under {src}")
        print(f"Found {len(zips):,} zip(s) and {len(loose):,} loose json file(s)")
        for zp in zips:
            yield from iter_zip(zp)
        for p in loose:
            yield str(p), p.read_bytes()
    else:
        sys.exit(f"Not a zip or folder: {src}")


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path,
                    help="a zip file, or a folder searched recursively for zips")
    ap.add_argument("-o", "--output", default="remedial",
                    help="output filename prefix (default: remedial)")
    a = ap.parse_args()

    total = bad = 0
    seen_events = set()                 # guards against the same event in two zips
    permit_count = defaultdict(set)     # promoter -> permit refs submitted
    remedial = {}                       # remedial permit ref -> row
    failed = {}                         # permit ref -> set of "refused"/"cancelled"
    FAIL = {"PERMIT_REFUSED": "refused", "PERMIT_CANCELLED": "cancelled"}

    for name, raw in iter_json(a.source):
        total += 1
        try:
            ev = json.loads(raw)
            od = ev.get("object_data") or {}
        except ValueError:
            bad += 1
            continue
        etype = ev.get("event_type", "")
        if etype != "PERMIT_SUBMITTED" and etype not in FAIL:
            continue
        ref = od.get("permit_reference_number") or ev.get("object_reference") or ""
        key = (ref, etype, ev.get("event_time", ""))
        if key in seen_events:
            continue
        seen_events.add(key)

        if etype in FAIL:
            failed.setdefault(ref, set()).add(FAIL[etype])
            continue
        if (od.get("work_category_ref") or "").lower() == "paa":
            continue

        prom = od.get("promoter_organisation", "")
        permit_count[prom].add(ref)
        if (od.get("activity_type") or "").strip().lower() == ACTIVITY:
            m = SUFFIX_RE.match(ref)
            row = {k: od.get(k, "") for k in PERMIT_FIELDS}
            row.update(permit_reference_number=ref,
                       work_reference_number=od.get("work_reference_number")
                       or (m.group(1) if m else ref),
                       suffix=int(m.group(2)) if m else "")
            remedial[ref] = row

    rows = list(remedial.values())
    for r in rows:
        f = failed.get(r["permit_reference_number"], ())
        r["refused"] = "yes" if "refused" in f else "no"
        r["cancelled"] = "yes" if "cancelled" in f else "no"
    rows.sort(key=lambda r: (r["work_reference_number"], r["suffix"] or 0))

    def buckets(rs):
        """works with 1, 2, 3, 4+ remedial permits among rows rs."""
        per_work = defaultdict(int)
        for r in rs:
            if COUNT_FAILED_AS_REVISIT or (r["refused"] == "no" and r["cancelled"] == "no"):
                per_work[r["work_reference_number"]] += 1
        out = [0] * MAX_BUCKET
        for n in per_work.values():
            out[min(n, MAX_BUCKET) - 1] += 1
        return out

    pct = lambda n, d: round(100 * n / d, 1) if d else 0.0
    by_prom = defaultdict(list)
    for r in rows:
        by_prom[r["promoter_organisation"]].append(r)

    def summarise(label, n_permits, rs):
        row = {"promoter_organisation": label, "permits": n_permits,
               "remedial_permits": len(rs), "remedial_pct": pct(len(rs), n_permits),
               "remedial_refused": sum(r["refused"] == "yes" for r in rs),
               "remedial_cancelled": sum(r["cancelled"] == "yes" for r in rs)}
        row.update(zip(BUCKETS, buckets(rs)))
        return row

    prom_rows = sorted((summarise(k, len(v), by_prom.get(k, []))
                        for k, v in permit_count.items()),
                       key=lambda r: -r["permits"])
    overall = summarise("ALL PROMOTERS", sum(len(v) for v in permit_count.values()), rows)

    write_csv(f"{a.output}_permits.csv", PERMIT_FIELDS, rows)
    write_csv(f"{a.output}_by_promoter.csv", PROMOTER_FIELDS, prom_rows + [overall])

    print(f"\nScanned {total:,} files ({bad:,} unreadable)\n")
    print(f"Permits submitted (excl. PAAs) : {overall['permits']:>8,}")
    print(f"Remedial permits               : {overall['remedial_permits']:>8,}"
          f"   = {overall['remedial_pct']}% of permits")
    print(f"    of which refused           : {overall['remedial_refused']:>8,}")
    print(f"    of which cancelled         : {overall['remedial_cancelled']:>8,}")
    print("\nRe-visits (remedial permits logged per works"
          + ("" if COUNT_FAILED_AS_REVISIT else ", refused/cancelled left out") + "):")
    for i, b in enumerate(BUCKETS, 1):
        lab = f"{i}{' or more' if i == MAX_BUCKET else ''} remedial{'s' if i > 1 else ''} logged"
        print(f"  {lab:<28}{overall[b]:>8,} works")

    hdr = "".join(f"{str(i) + ('+' if i == MAX_BUCKET else ''):>6}" for i in range(1, MAX_BUCKET + 1))
    print(f"\n  {'Promoter (top 15 by permits)':<40}{'Permits':>9}{'Remedial':>10}{'%':>7}  |{hdr}")
    for r in prom_rows[:15]:
        print(f"  {r['promoter_organisation'][:38]:<40}{r['permits']:>9,}"
              f"{r['remedial_permits']:>10,}{r['remedial_pct']:>7}  |"
              + "".join(f"{r[b]:>6,}" for b in BUCKETS))
    print(f"\nWrote {a.output}_permits.csv and {a.output}_by_promoter.csv")


if __name__ == "__main__":
    main()
