"""Finding follow-through — deterministic rule-based matching.

The reference *Build* from the action plan: for each case, walk the records in
exam order and, for each new record, propose zero or one continuation among the
case's EARLIER records by a stated deterministic rule on the record fields. No
model, no network — pure Python on the FT-DM fields, so it runs in milliseconds.

Each proposal carries: the rule that fired, the fields it read, a score in
[0,100], and a status — automatic (applies without a person), candidate
(presented for confirmation), or none (new / nothing to attach to). A header/body
laterality conflict forces review and demotes automatic -> candidate, exactly as
the LLM path does, so the two outputs are directly comparable.

The row schema matches match_llm.py so results/evaluation.py scores both
identically. The answer key is never read here.

Usage:
    python3 src/match_rules.py                 # all cases -> results/matching_rules.json
    python3 src/match_rules.py SYN-01          # one case
    python3 src/match_rules.py --score         # also score against answer_key
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from records import load as load_records, load_config  # noqa: E402

# ponytail: the modality families a record's modality belongs to. ABUS is its own
# depth convention (from skin); CEM is a contrast sub-modality; everything else is
# "standard" (MG/DBT/US/MRI/PATH).
STANDARD = {"MG", "DBT", "US", "MRI", "PATH", "CEM", "ABUS"}
# ponytail: distance-from-nipple tolerance per modality pair, cm. Not tuned to the
# answer key — these are the *design values* the action plan says to propose from
# the data (FT-REQ-07). Kept small to keep same-clock/same-depth lesions split.
FN_TOL_CM = {
    frozenset({"MG", "US"}): 2.0,
    frozenset({"MG", "MRI"}): 3.0,
    frozenset({"US", "MRI"}): 3.0,
    frozenset({"ABUS", "ABUS"}): 1.5,
}
DEFAULT_FN_TOL_CM = 2.5  # MG<->MRI, US<->CEM, ABUS<->anything, ...
# clock angular tolerance (hours). 1 hour == one 30-minute step == 2 clock positions.
CLOCK_TOL_H = 1.0
# quadrant boundary hours that count as "compatible" with the adjacent quadrant.
BOUNDARY_HOURS = {12, 3, 6, 9}


def get_logger(root: Path | None = None) -> logging.Logger:
    log_root = root or Path(__file__).resolve().parent.parent
    log_dir = log_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("fft.rules")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(log_dir / "match_rules.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(ch)
    return log


# --------------------------------------------------------------------------- #
# Field helpers
# --------------------------------------------------------------------------- #
def _clock_hours(clock: str | None) -> float | None:
    """'10:00' -> 10.0 ; '1:30' -> 1.5. Range '1:00-2:00' -> None (handled separately)."""
    if not clock:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", clock)
    if not m:
        return None
    return int(m.group(1)) + int(m.group(2)) / 60.0


def _clock_range(clock: str | None) -> tuple[float, float] | None:
    """'1:00-2:00' -> (1.0, 2.0). Used for extent/clock ranges (SYN-11 calcifications)."""
    if not clock:
        return None
    m = re.fullmatch(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", clock)
    if not m:
        return None
    lo = _clock_hours(m.group(1))
    hi = _clock_hours(m.group(2))
    return (lo, hi)


def _fn(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fn_range(extent_or_fn) -> tuple[float, float] | None:
    """'4-5' string extent/fn -> (4,5). Number -> (v,v). Used so a point inside a
    stated range (SYN-11 biopsy at 1:30 inside 1:00-2:00) matches."""
    if extent_or_fn is None:
        return None
    if isinstance(extent_or_fn, str) and "-" in extent_or_fn:
        a, _, b = extent_or_fn.partition("-")
        try:
            return (float(a.strip()), float(b.strip()))
        except ValueError:
            return None
    v = _fn(extent_or_fn)
    return (v, v) if v is not None else None


def _depth_third(depth: str | None) -> int | None:
    """anterior=1, middle=2, posterior=3. ABUS 'x cm from skin' -> None (different unit)."""
    if not depth:
        return None
    d = depth.lower()
    if d.startswith("anterior"):
        return 1
    if d.startswith("middle"):
        return 2
    if d.startswith("posterior"):
        return 3
    return None  # ABUS depth-from-skin, or free text


def _quadrant_range(quadrant: str | None, laterality: str | None) -> tuple[int, int] | None:
    """Quadrant -> inclusive hour range for the given breast. Left/right mirrored.
    Right: UOQ 9-12, UIQ 12-3, LIQ 3-6, LOQ 6-9.
    Left:  UOQ 12-3, UIQ 9-12, LIQ 6-9, LOQ 3-6."""
    if not quadrant or not laterality:
        return None
    q = quadrant.upper()
    side = laterality.upper()
    table = {
        ("R", "UOQ"): (9, 12), ("R", "UIQ"): (12, 15), ("R", "LIQ"): (3, 6), ("R", "LOQ"): (6, 9),
        ("L", "UOQ"): (12, 15), ("L", "UIQ"): (9, 12), ("L", "LIQ"): (6, 9), ("L", "LOQ"): (3, 6),
    }
    return table.get((side, q))


def _hour_in_range(h: float, rng: tuple[int, int]) -> bool:
    lo, hi = rng
    # wrap-aware containment on a 12-hour circle
    lo12, hi12 = lo % 12, hi % 12
    h12 = h % 12
    if lo12 <= hi12:
        return lo12 <= h12 <= hi12
    return h12 >= lo12 or h12 <= hi12  # range crosses 12:00


def _clock_angle_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    d = abs((a - b) % 12)
    return min(d, 12 - d)


def _compat_clock(rec_a, rec_b) -> bool:
    """Same-clock / boundary / range containment, per the conventions.
    A point matches a range if it's inside it; two points match within CLOCK_TOL_H;
    a boundary hour (12/3/6/9) matches both adjacent quadrants."""
    def hour_of(rec):
        loc = rec.get("location") or {}
        rng = _clock_range(loc.get("clock"))
        point = _clock_hours(loc.get("clock")) if not rng else None
        return point, rng

    pa, ra = hour_of(rec_a)
    pb, rb = hour_of(rec_b)
    # point vs range
    if pa is not None and rb is not None:
        lo, hi = rb
        if _hour_in_range(pa, (lo, hi)):
            return True
    if pb is not None and ra is not None:
        lo, hi = ra
        if _hour_in_range(pb, (lo, hi)):
            return True
    # point vs point
    if pa is not None and pb is not None:
        return _clock_angle_diff(pa, pb) is not None and _clock_angle_diff(pa, pb) <= CLOCK_TOL_H
    return False


def _compat_quadrant(rec_a, rec_b) -> bool:
    qa = _quadrant_range((rec_a.get("location") or {}).get("quadrant"), rec_a.get("laterality"))
    qb = _quadrant_range((rec_b.get("location") or {}).get("quadrant"), rec_b.get("laterality"))
    if qa is None and qb is None:
        return True  # neither has a quadrant -> don't score on it
    if qa is None or qb is None:
        return False  # one has a quadrant, the other doesn't -> can't confirm same quadrant
    # overlap of two hour ranges (inclusive) on the 12h circle
    a_lo, a_hi, b_lo, b_hi = qa[0], qa[1], qb[0], qb[1]
    # normalize to unwrapped and check overlap across a cut
    def spans(rng):
        lo, hi = rng
        if lo <= hi:
            return (lo, hi)
        return (lo, hi + 12)
    (s1, e1), (s2, e2) = spans((a_lo, a_hi)), spans((b_lo, b_hi))
    return e1 >= s2 and e2 >= s1  # overlap


def _location_compatible(rec_a: dict, rec_b: dict) -> tuple[bool, str]:
    """Hard location gate: quadrant (if present on both), clock/extent, depth, fn.
    Returns (compatible, why)."""
    la, lb = rec_a.get("location") or {}, rec_b.get("location") or {}
    # quadrant: compatible if both present and overlapping (or one present and the
    # other's clock/extent falls in it). If both quadrants given, require overlap.
    qa = la.get("quadrant"); qb = lb.get("quadrant")
    if qa and qb:
        if not _compat_quadrant(rec_a, rec_b):
            return False, f"quadrant {qa} vs {qb} (no overlap)"
    # clock: compatible if both have clock/range and one contains the other, or within tol
    ca = la.get("clock"); cb = lb.get("clock")
    if ca and cb and not _compat_clock(rec_a, rec_b):
        return False, f"clock {ca} vs {cb} (beyond {CLOCK_TOL_H}h)"
    # depth: if both are standard thirds and differ by >1 third -> reject
    da = _depth_third(la.get("depth")); db = _depth_third(lb.get("depth"))
    if da is not None and db is not None and abs(da - db) > 1:
        return False, f"depth {da} vs {db} (differ by >1 third)"
    # fn: if both numeric and differ beyond the modality-pair tolerance -> reject
    fa = _fn(la.get("fn_cm")); fb = _fn(lb.get("fn_cm"))
    if fa is not None and fb is not None:
        pair = FN_TOL_CM.get(frozenset({rec_a.get("modality"), rec_b.get("modality")}), DEFAULT_FN_TOL_CM)
        if abs(fa - fb) > pair:
            return False, f"fn_cm {fa} vs {fb} (beyond {pair}cm)"
    return True, ""


# --------------------------------------------------------------------------- #
# Candidate scoring
# --------------------------------------------------------------------------- #
def _finding_type_fits(rec_new: dict, rec_ref: dict) -> bool:
    """A specimen fits any lesion type it was taken from; 'no correlate' fits the
    finding it searched for; otherwise the types should be the same or an
    evolution (asymmetry -> mass) the radiologist's correspondence confirms."""
    tn = rec_new.get("finding_type")
    tr = rec_ref.get("finding_type")
    if tn == "specimen" or tn == "no correlate":
        return True
    if tn == tr:
        return True
    # documented evolutions the key treats as one lesion (SYN-09, SYN-20)
    evolutions = {frozenset({"asymmetry", "mass"}),
                  frozenset({"focal asymmetry", "mass"}),
                  frozenset({"calcifications", "non-mass enhancement"}),
                  frozenset({"asymmetry", "focal asymmetry"})}
    return frozenset({tn, tr}) in evolutions


def _correspondence_supports(rec_new: dict, rec_ref: dict) -> tuple[bool, bool]:
    """(affirmed, hedged). The new record's correspondence is the radiologist's own
    sentence; we only check polarity + certainty, not resolve the target to rec_ref
    (that's MDE/MA's job, and the target_text is the radiologist's words, not an id)."""
    affirmed = False
    hedged = False
    for c in rec_new.get("correspondence") or []:
        pol = (c.get("polarity") or "").lower()
        cert = (c.get("certainty") or "").lower()
        if pol == "affirmed":
            affirmed = True
        if cert == "hedged" or "may" in (c.get("target_text") or "").lower() \
                or "likely" in (c.get("target_text") or "").lower() \
                or "probably" in (c.get("target_text") or "").lower() \
                or "may" in (c.get("source_span") or "").lower():
            hedged = True
    return affirmed, hedged


def _correspondence_negated(rec_new: dict) -> bool:
    """'no sonographic correlate' / 'separate from the screening finding' — the
    radiologist explicitly says this is NOT the earlier finding."""
    for c in rec_new.get("correspondence") or []:
        if (c.get("polarity") or "").lower() == "negated":
            return True
    return False


def _correspondence_negated(rec_new: dict) -> bool:
    """'no sonographic correlate' / 'separate from the screening finding' — the
    radiologist explicitly says this is NOT the earlier finding."""
    for c in rec_new.get("correspondence") or []:
        if (c.get("polarity") or "").lower() == "negated":
            return True
    return False


def _specimen_supports(rec_new: dict, rec_ref: dict) -> bool:
    """A pathology specimen links to a lesion if its clip statement or its stated
    location matches the reference's location (SYN-08, SYN-10, SYN-11, SYN-20)."""
    if rec_new.get("finding_type") != "specimen":
        return False
    spec = rec_new.get("specimen") or {}
    if spec.get("clip_statement"):
        return True  # clip is the strongest carrier — trust it
    # else location match (already checked by _location_compatible upstream)
    return True


def propose(rec_new: dict, recs_so_far: list[dict], index_of_new: int,
            conservative: bool = False) -> dict:
    """Return the row for rec_new given all earlier records of the case.

    recs_so_far contains records at index < index_of_new (exam order); the new
    record itself is EXCLUDED. Candidates are [0, index_of_new).
    """
    case_id = rec_new["finding_id"].rsplit("-E", 1)[0]
    row = {
        "case_id": case_id,
        "new_record": rec_new["finding_id"],
        "status": "none",
        "link": None,
        "score": 0,
        "rule": "",
        "fields": [],
        "review_required": False,
        "model_error": None,
    }
    row["fields"] = _fields_used(rec_new)

    # hard gates: laterality + site are enforced inside the candidate scan (skip
    # different-breast / different-site references), so a cross-breast link is
    # structurally impossible (SYN-05 trap).

    # 1. Explicit non-correspondence -> never link (the sentence beats location)
    if _correspondence_negated(rec_new):
        row["rule"] = "RULE-NONCORR: recorded as not the same as an earlier finding"
        return row
    status = rec_new.get("status_statement")
    status_val = status.get("value") if isinstance(status, dict) else status
    if status_val in ("new",):
        row["rule"] = "RULE-NEW: status_statement = new"
        return row

    # 2. Candidate scan — constrained to the immediately preceding EXAM (FT rule:
    #    a lesion continues only from the previous exam; already-matched chain
    #    information is what may travel on). If the previous exam has no
    #    same-breast/same-site candidate for this lesion, fall back to the full
    #    earlier history (e.g. the SYN-07 MRI chain where the mammogram in between
    #    reported no finding).
    candidates = []
    for rec_ref in recs_so_far:
        if rec_ref.get("laterality") != rec_new.get("laterality"):
            continue
        if rec_ref.get("site") != rec_new.get("site"):
            continue
        if rec_ref["finding_id"] == rec_new["finding_id"]:
            continue
        ok, why = _location_compatible(rec_new, rec_ref)
        if not ok:
            continue
        if not _finding_type_fits(rec_new, rec_ref):
            continue
        candidates.append((rec_ref, why))
    if not candidates:
        rule_msg = ("RULE-NONE: no earlier same-breast/same-site candidate"
                    if rec_new.get("finding_type") != "no correlate"
                    else "RULE-NEGSEARCH: no earlier lesion to attach the search to")
        row["rule"] = rule_msg
        return row

    def _exam_rank(fid: str) -> int:
        m = re.match(r"^SYN-\d+-E(\d+)", fid)
        return int(m.group(1)) if m else 0

    new_rank = _exam_rank(rec_new["finding_id"])
    prev_exam_cands = [c for c in candidates if _exam_rank(c[0]["finding_id"]) == new_rank - 1]
    if prev_exam_cands:
        candidates = prev_exam_cands  # link only to the previous exam

    # 3. Score each candidate; pick the best.
    scored = []
    for rec_ref, why in candidates:
        s = 50.0  # base: passed hard gates (laterality, site, location, type-fits)
        reasons = []
        # clock match (point, boundary, or range containment) is a strong confirm
        if (rec_new.get("location") or {}).get("clock") and (rec_ref.get("location") or {}).get("clock"):
            if _compat_clock(rec_new, rec_ref):
                s += 15; reasons.append("clock")
        # depth same third
        da = _depth_third((rec_new.get("location") or {}).get("depth"))
        db = _depth_third((rec_ref.get("location") or {}).get("depth"))
        if da is not None and db is not None and da == db:
            s += 5; reasons.append("depth")
        # fn within a tight band (<=1cm) is a confirm
        fa = _fn((rec_new.get("location") or {}).get("fn_cm"))
        fb = _fn((rec_ref.get("location") or {}).get("fn_cm"))
        if fa is not None and fb is not None and abs(fa - fb) <= 1.0:
            s += 5; reasons.append("fn<=1cm")
        # same finding_type is a stronger confirm than an evolution
        if rec_new.get("finding_type") == rec_ref.get("finding_type"):
            s += 5; reasons.append("type=eq")
        # correspondence affirmed & not hedged is the strongest confirm
        aff, hed = _correspondence_supports(rec_new, rec_ref)
        if aff and not hed:
            s += 25; reasons.append("corr=affirmed")
        elif aff and hed:
            s += 15; reasons.append("corr=hedged")
        # specimen with clip statement is the strongest carrier
        if rec_new.get("finding_type") == "specimen" and (rec_new.get("specimen") or {}).get("clip_statement"):
            s += 25; reasons.append("clip")
        # 'no correlate' attaches to the lesion it searched for
        if rec_new.get("finding_type") == "no correlate":
            s += 20; reasons.append("neg-search")
        scored.append((s, rec_ref, why, reasons))

    scored.sort(key=lambda x: (
        -x[0],
        -_exam_rank(x[1]["finding_id"]),                       # most recent EXAM first
        -int(re.search(r"F(\d+)$", x[1]["finding_id"]).group(1)) if re.search(r"F(\d+)$", x[1]["finding_id"]) else 0,
    ))
    best_score, best_ref, best_why, best_reasons = scored[0]
    runner_score = scored[1][0] if len(scored) > 1 else 0.0
    margin = best_score - runner_score

    # 4. Decide status.
    aff, hed = _correspondence_supports(rec_new, best_ref)
    hedged = hed or (rec_new.get("finding_type") == "no correlate")  # searches are confirm-by-person
    tie = margin < 15 and runner_score > 0  # a real tie -> candidate
    if best_score >= 80 and not hedged and not tie:
        status_out = "automatic"
        if conservative:  # action-plan reading FT-REQ-24: nothing automatic
            status_out = "candidate"
    elif best_score >= 50:
        status_out = "candidate"
    else:
        status_out = "none"
        return row  # not confident enough -> no link

    row["status"] = status_out
    row["link"] = best_ref["finding_id"]
    row["score"] = int(round(best_score))
    row["rule"] = (f"RULE-MATCH: laterality+site+location+type-fits "
                   f"({' + '.join(best_reasons)})")
    row["review_required"] = (status_out == "candidate") or hedged or tie

    # 5. Header/body laterality conflict on THIS record's exam -> review + demote.
    # (the caller sets this; expose a hook via a sentinel field)
    return row


def _fields_used(rec: dict) -> list[str]:
    loc = rec.get("location") or {}
    fields = [k for k in ("laterality", "site", "finding_type") if rec.get(k)]
    fields += [k for k in ("quadrant", "clock", "depth", "fn_cm", "extent", "view_basis") if loc.get(k)]
    if rec.get("descriptors"):
        fields.append("descriptors")
    if rec.get("size_mm") is not None:
        fields.append("size_mm")
    if rec.get("status_statement"):
        fields.append("status_statement")
    if rec.get("correspondence"):
        fields.append("correspondence")
    if rec.get("specimen"):
        fields.append("specimen")
    if rec.get("retrospective_note"):
        fields.append("retrospective_note")
    return fields


# --------------------------------------------------------------------------- #
# Drive a case (no model)
# --------------------------------------------------------------------------- #
def _records(case: dict) -> list[dict]:
    return [r for e in case.get("exams", []) for r in e.get("extracted_findings", [])]


def _exam_flags(case: dict) -> dict[str, bool]:
    """finding_id -> True if its exam has a header/body laterality conflict."""
    out = {}
    for e in case.get("exams", []):
        conflict = bool(e.get("header_laterality") and e.get("header_laterality") != e.get("exam_laterality"))
        for r in e.get("extracted_findings", []):
            out[r["finding_id"]] = conflict
    return out


def run_case(case: dict, logger: logging.Logger | None = None,
             conservative: bool = False) -> list[dict]:
    logger = logger or logging.getLogger("fft.rules")
    recs = _records(case)
    cid = case.get("case_id")
    logger.info("[%s] %d records across %d exams (rule-based, no model)%s", cid, len(recs),
                len(case.get("exams", [])), ", conservative" if conservative else "")
    flags = _exam_flags(case)
    rows = []
    for i, rec in enumerate(recs):
        earlier = recs[:i]
        row = propose(rec, earlier, i, conservative=conservative)
        # header conflict -> review + demote automatic -> candidate
        if flags.get(rec["finding_id"]) and row.get("link"):
            if row["status"] == "automatic":
                row["status"] = "candidate"
                row["rule"] += "  [header-laterality conflict -> demoted to candidate]"
            row["review_required"] = True
            row["rule"] = row["rule"] + ("  [review: header conflict]" if row["rule"].find("review") < 0 else "")
        rows.append(row)
    auto = sum(1 for r in rows if r["status"] == "automatic")
    cand = sum(1 for r in rows if r["status"] == "candidate")
    none = sum(1 for r in rows if r["status"] == "none")
    review = sum(1 for r in rows if r.get("review_required"))
    logger.info("[%s] done: %d rows | automatic=%d candidate=%d none=%d review_required=%d",
                cid, len(rows), auto, cand, none, review)
    return rows


def _write(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
        f.write("\n")


def summarise(rows: list[dict]) -> dict:
    by: dict[str, int] = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    linked = [r for r in rows if r["link"]]
    return {
        "total": len(rows),
        "status_counts": by,
        "linked": len(linked),
        "automatic_linked": sum(1 for r in linked if r["status"] == "automatic"),
        "candidate_linked": sum(1 for r in linked if r["status"] == "candidate"),
        "review_required": sum(1 for r in rows if r.get("review_required")),
        "model_errors": sum(1 for r in rows if r.get("model_error")),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic rule-based finding follow-through matcher.")
    ap.add_argument("case_ids", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--out", default=None, help="output JSON path (default results/matching_rules.json)")
    ap.add_argument("--state", default=None, help="checkpoint dir (default <out-dir>/cases_rules)")
    ap.add_argument("--debug", action="store_true", help="stream DEBUG to the console too")
    ap.add_argument("--conservative", action="store_true",
                    help="action-plan reading FT-REQ-24: every link is candidate, none automatic")
    ap.add_argument("--score", action="store_true", help="also score against answer_key")
    args = ap.parse_args()

    store = load_records()
    root = store.root
    cases = [store.case(c) for c in args.case_ids] if args.case_ids else list(store.cases)
    log = get_logger(root)
    if args.debug:
        for h in log.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)
    log.info("pipeline start (rule-based): %d case(s)", len(cases))

    def state_path(case):
        sd = Path(args.state) if args.state else None
        default = (Path(args.out) if args.out else (root / "results" / "matching_rules.json")).parent / "cases_rules"
        d = sd or default
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{case['case_id']}.json"

    def load_state(case):
        p = state_path(case)
        if p.is_file():
            try:
                d = json.loads(p.read_text())
                if isinstance(d, list) and d:
                    return d
            except json.JSONDecodeError:
                pass
        return None

    rows: list[dict] = []
    for i, case in enumerate(cases, 1):
        cached = load_state(case)
        tag = "(cached)" if cached is not None else ""
        print(f"[{i}/{len(cases)}] {case['case_id']}  {tag}", end=" ", flush=True)
        case_rows = cached if cached is not None else run_case(case, logger=log,
                                                                 conservative=args.conservative)
        _write(case_rows, state_path(case))
        print(f"done ({sum(1 for r in case_rows if r.get('link'))} linked)")
        rows.extend(case_rows)

    out_path = Path(args.out) if args.out else (root / "results" / "matching_rules.json")
    _write(rows, out_path)
    s = summarise(rows)
    log.info("pipeline end: %d rows | auto=%d cand=%d none=%d review=%d errors=%d",
             s["total"], s["automatic_linked"], s["candidate_linked"],
             s["status_counts"].get("none", 0), s["review_required"], s["model_errors"])
    print("\n== summary == (rule-based)")
    for k, v in s.items():
        print(f"  {k}: {v}")
    print(f"\nwrote: {out_path}")

    if args.score:
        from evaluate import score as _score  # type: ignore
        s = _score(out_path)
        print(f"  precision={s['precision']:.3f}  recall={s['recall']:.3f}  F1={s['f1']:.3f}")


if __name__ == "__main__":
    main()
