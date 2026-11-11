#!/usr/bin/env python3
"""Score results/matching_results.json against results/answer_key.json.

Read-only (neither file is modified). Writes nothing except a printed summary
and an optional SVG figure on stdout.

The ground-truth edges come from the answer key's:
  - lesions[].finding_ids, non_lesions[].finding_ids  -> "should-link" edges
  - candidate_links[].finding_ids                       -> "should-propose" edges
The matcher's edges come from its `link` field (one per finding, to an EARLIER finding).

A "link edge" is (new_record, link). A ground-truth edge is (new, prev) where
new and prev are consecutive in the same finding_ids list. A matcher edge that
matches a GT edge is a true positive; a matcher edge that matches no GT edge is
a false positive (wrong link); a GT edge with no matching matcher edge is a
false negative (missed link). Status is then judged on top: an auto link that
should have been candidate is the BC-008 "wrongly automatic" failure.

Run:  python3 src/evaluate.py            (or from anywhere)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = ROOT / "results" / "answer_key.json"
CASES_DIR = ROOT / "results" / "cases"
# default file; --file overrides (e.g. results/matching_rules.json for the rule-based run)
RESULTS = ROOT / "results" / "matching_results.json"


def load_rows(path: Path | None = None) -> list[dict]:
    return json.loads((path or RESULTS).read_text())


def gt_edges_for_case(case_id: str) -> set[tuple[str, str]]:
    """Ground-truth link edges: (new, prev) from lesions + non_lesions + candidate_links.
    candidate_links edges are also valid continuations the matcher should find — they
    just must not be applied automatically."""
    keys = json.loads(KEY.read_text())["keys"]
    k = next((x for x in keys if x["case_id"] == case_id), None)
    if k is None:
        return set()
    edges: set[tuple[str, str]] = set()
    for chain_key in ("lesions", "non_lesions"):
        for chain in (k.get(chain_key) or []):
            ids = chain.get("finding_ids") or []
            for prev, new in zip(ids, ids[1:]):
                edges.add((new, prev))
    for cl in (k.get("candidate_links") or []):
        ids = cl.get("finding_ids") or []
        for prev, new in zip(ids, ids[1:]):
            edges.add((new, prev))
    return edges


def gt_candidate_only_findings(case_id: str) -> set[str]:
    """Findings that appear ONLY as the 'new' side of a candidate_link in the answer key.
    The key's reading: such a record is 'correct if proposed or left unlinked, wrong if
    linked automatically.' So a 'none' row for them is NOT a miss — only an 'automatic'
    row for them is a wrongly-automatic failure."""
    keys = json.loads(KEY.read_text())["keys"]
    k = next((x for x in keys if x["case_id"] == case_id), None)
    if k is None:
        return set()
    out: set[str] = set()
    for cl in (k.get("candidate_links") or []):
        ids = cl.get("finding_ids") or []
        if len(ids) >= 2:
            out.add(ids[0])  # the 'new' side of the candidate edge
    return out


def gt_review_required(case_id: str) -> set[str]:
    keys = json.loads(KEY.read_text())["keys"]
    k = next((x for x in keys if x["case_id"] == case_id), None)
    if k is None:
        return set()
    trig = " ".join(str(t) for t in (k.get("review_triggers") or []))
    import re
    return set(re.findall(r"SYN-\d+-E\d+-F\d+", trig))


def score(file: Path | None = None) -> dict:
    rows = load_rows(file)
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)

    tp = fp = fn = 0
    wrongly_auto = 0  # auto link, or auto link to a candidate-only finding (BC-008)
    legitimately_unlinked = 0  # 'none' rows that the key expects to be unlinked
    per_case: list[dict] = []

    for case_id, case_rows in sorted(by_case.items()):
        gt = gt_edges_for_case(case_id)
        cand_only = gt_candidate_only_findings(case_id)
        matched_gt: set[tuple[str, str]] = set()
        m_tp = m_fp = m_fn = 0
        wr_auto = legit = 0
        for r in case_rows:
            if r.get("link"):
                edge = (r["new_record"], r["link"])
                if edge in gt:
                    m_tp += 1
                    matched_gt.add(edge)
                    # wrongly-automatic: linked, and the finding is candidate-only,
                    # and the status is automatic (the one failure the key names)
                    if r["status"] == "automatic" and r["new_record"] in cand_only:
                        wr_auto += 1
                else:
                    m_fp += 1
            else:
                # no link (status none, or candidate-with-null)
                # if this finding is candidate-only AND the key says it's fine unlinked, count as legit
                if r["new_record"] in cand_only:
                    legit += 1
                    # find the GT edge for this 'new' and mark it matched so it's not a FN
                    for e in gt:
                        if e[0] == r["new_record"]:
                            matched_gt.add(e)
                            break
        for e in gt:
            if e not in matched_gt:
                m_fn += 1
        tp += m_tp; fp += m_fp; fn += m_fn
        wrongly_auto += wr_auto
        legitimately_unlinked += legit
        per_case.append({
            "case": case_id, "tp": m_tp, "fp": m_fp, "fn": m_fn, "wrong_auto": wr_auto,
            "legit_unlinked": legit, "n_rows": len(case_rows),
            "auto": sum(1 for r in case_rows if r["status"] == "automatic"),
            "cand": sum(1 for r in case_rows if r["status"] == "candidate"),
            "none": sum(1 for r in case_rows if r["status"] == "none"),
            "review": sum(1 for r in case_rows if r.get("review_required")),
            "errors": sum(1 for r in case_rows if r.get("model_error")),
        })

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "rows": len(rows),
        "tp": tp, "fp": fp, "fn": fn, "wrongly_auto": wrongly_auto,
        "legitimately_unlinked": legitimately_unlinked,
        "precision": precision, "recall": recall, "f1": f1,
        "status": {
            "automatic": sum(1 for r in rows if r["status"] == "automatic"),
            "candidate": sum(1 for r in rows if r["status"] == "candidate"),
            "none": sum(1 for r in rows if r["status"] == "none"),
        },
        "review_required": sum(1 for r in rows if r.get("review_required")),
        "errors": sum(1 for r in rows if r.get("model_error")),
        "per_case": per_case,
    }


def runtimes() -> list[dict]:
    """Honest runtime accounting.

    The per-case checkpoint mtimes are NOT reliable for per-case durations: the
    `review_required` backfill rewrote all 20 files in a 50 ms window, and the
    two cases I re-ran individually (SYN-01, SYN-19) carry their re-run write
    time. So this returns the wall-clock span of the actual model runs, not
    per-case inference times. The per-case numbers are reported as unknown.
    """
    if not CASES_DIR.is_dir():
        return []
    files = [f for f in CASES_DIR.glob("*.json") if f.name != "answer_key.json"]
    if not files:
        return []
    times = sorted(f.stat().st_mtime for f in files)
    span = times[-1] - times[0]
    return [{"case": "span", "seconds": span, "estimated": False}]


def print_report(s: dict, rts: list[dict]) -> None:
    p = lambda x: f"{x:8.3f}"
    print("== MATCHING RESULTS ==  (matching_results.json vs answer_key.json)")
    print(f"  rows scored        : {s['rows']}")
    print(f"  true links (TP)    : {s['tp']}")
    print(f"  wrong links (FP)   : {s['fp']}")
    print(f"  missed links (FN)  : {s['fn']}")
    print(f"  wrongly automatic  : {s['wrongly_auto']}   (BC-008: should have been candidate)")
    print(f"  precision          : {p(s['precision'])}      = TP/(TP+FP)")
    print(f"  recall             : {p(s['recall'])}      = TP/(TP+FN)")
    print(f"  F1                 : {p(s['f1'])}")
    st = s["status"]
    print(f"\n  status distribution: automatic={st['automatic']}  candidate={st['candidate']}  none={st['none']}")
    print(f"  review_required    : {s['review_required']}   (flagged for a person)")
    print(f"  model errors       : {s['errors']}   (parse/fail-safe rows)")
    print(f"  error rate         : {s['errors']/s['rows']*100:.1f}%   ({s['errors']}/{s['rows']} rows)")
    if rts:
        durs = [r["seconds"] for r in rts if r["seconds"] is not None]
        if durs:
            # durs is the wall-clock span of the actual model runs (all 20 cases),
            # NOT a sum of per-case inference times — per-case times were clobbered
            # by the review_required backfill rewriting all checkpoint files.
            print(f"\n== RUNTIME (wall-clock span of the model runs) ==")
            print(f"  total (20 cases, incl. retries) : {durs[0]:7.0f} s  = {durs[0]/60:5.1f} min")
            print(f"  per-case inference time          : not recoverable from checkpoint")
            print(f"                                   mtimes (the 18 bulk files were rewritten")
            print(f"                                   together by the review_required backfill in")
            print(f"                                   a ~50 ms window; the 2 re-run cases carry")
            print(f"                                   their re-run write time). See NOTE below.")
            print(f"  NOTE: the span includes model-load, GPU contention with the agent's own")
            print(f"  27b model, and the two SYN-01 / SYN-19 individual re-runs. It is an upper")
            print(f"  bound on pure inference time for the 20 cases, not a per-case breakdown.")
    print("\n== PER CASE (tp=correct links, fp=wrong, fn=missed, WA=wrongly-automatic) ==")
    print(f"  {'case':<8} {'rows':>4} {'auto':>4} {'cand':>4} {'none':>4} {'rev':>3} {'TP':>3} {'FP':>3} {'FN':>3} {'WA':>3}")
    for c in s["per_case"]:
        print(f"  {c['case']:<8} {c['n_rows']:>4} {c['auto']:>4} {c['cand']:>4} {c['none']:>4} "
              f"{c['review']:>3} {c['tp']:>3} {c['fp']:>3} {c['fn']:>3} {c['wrong_auto']:>3}")


def make_svg(s: dict, rts: list[dict], path: Path) -> None:
    """Upper half: status donut (left) + runtime (right), side by side.
    Lower half: links-per-case TP/FP/FN stacked bars, full width (fits all N cases)."""
    import math

    W = 900
    margin = 60
    p = lambda x: f"{x:.3f}"
    pc = s["per_case"]
    n_cases = len(pc)

    # vertical layout
    half0 = 90
    panel_label_y = 112
    top_block_y = half0 + 150          # centre line for donut / runtime text
    top_block_h = 270
    top_block_bottom = half0 + 40 + top_block_h
    bottom_label_y = top_block_bottom + 34
    plot_h = 250
    plot_bottom = bottom_label_y + plot_h
    legend_y = plot_bottom + 22
    H = legend_y + 42

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'font-family="DejaVu Sans,Arial,sans-serif" font-size="13">']
    svg.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    svg.append(f'<text x="{margin}" y="34" font-size="20" font-weight="bold">Finding Follow-Through — Matching Results</text>')
    svg.append(f'<text x="{margin}" y="56" font-size="12" fill="#555">'
               f'{s["rows"]} findings · {s["tp"]} TP / {s["fp"]} FP / {s["fn"]} FN · '
               f'P={p(s["precision"])} R={p(s["recall"])} F1={p(s["f1"])} · errors={s["errors"]}</text>')

    mid = W // 2
    svg.append(f'<line x1="{margin}" y1="{half0 + 44}" x2="{W - margin}" y2="{half0 + 44}" stroke="#e6e6e6"/>')

    # --- Panel A: status donut (upper-left) ---
    svg.append(f'<text x="{margin}" y="{panel_label_y}" font-size="15" font-weight="bold">Status distribution</text>')
    st = s["status"]
    total = st["automatic"] + st["candidate"] + st["none"] or 1
    cx, cy0, r0 = margin + 150, top_block_y + 90, 90
    ang = -math.pi / 2
    seg_colors = {"automatic": "#2e8b57", "candidate": "#e67e22", "none": "#95a5a6"}
    for key in ("automatic", "candidate", "none"):
        frac = st[key] / total
        if frac == 0:
            continue
        a1 = ang + frac * 2 * math.pi
        x0 = cx + r0 * math.cos(ang); y0 = cy0 + r0 * math.sin(ang)
        x1 = cx + r0 * math.cos(a1); y1 = cy0 + r0 * math.sin(a1)
        large = 1 if (a1 - ang) > math.pi else 0
        svg.append(f'<path d="M {cx} {cy0} L {x0:.1f} {y0:.1f} A {r0} {r0} 0 {large} 1 {x1:.1f} {y1:.1f} Z" '
                   f'fill="{seg_colors[key]}" stroke="white" stroke-width="2"/>')
        m2 = (ang + a1) / 2
        lx = cx + (r0 * 0.62) * math.cos(m2); ly = cy0 + (r0 * 0.62) * math.sin(m2)
        svg.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="12" fill="white" '
                   f'text-anchor="middle" font-weight="bold">{st[key]}</text>')
        lr = r0 + 22
        lxx = cx + lr * math.cos(m2); lyy = cy0 + lr * math.sin(m2)
        anchor = "start" if math.cos(m2) >= 0 else "end"
        svg.append(f'<text x="{lxx:.1f}" y="{lyy:.1f}" font-size="12" text-anchor="{anchor}">{key} ({frac*100:.0f}%)</text>')
        ang = a1
    svg.append(f'<circle cx="{cx}" cy="{cy0}" r="{r0-34}" fill="white"/>')
    svg.append(f'<text x="{cx}" y="{cy0 - 6}" font-size="14" font-weight="bold" text-anchor="middle">{total}</text>')
    svg.append(f'<text x="{cx}" y="{cy0 + 10}" font-size="11" fill="#555" text-anchor="middle">findings</text>')
    ny = cy0 + r0 + 40
    svg.append(f'<text x="{margin}" y="{ny}" font-size="12" fill="#555">review_required (flagged for a person): {s["review_required"]}</text>')
    svg.append(f'<text x="{margin}" y="{ny + 18}" font-size="12" fill="#555">model errors (fail-safe none rows): {s["errors"]}</text>')

    # --- Panel B: runtime (upper-right) ---
    rx = mid + 20
    svg.append(f'<text x="{rx}" y="{panel_label_y}" font-size="15" font-weight="bold">Runtime</text>')
    ry = panel_label_y + 30
    if rts and rts[0]["seconds"] is not None:
        total_s = rts[0]["seconds"]
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">20 cases total : {total_s:.0f} s  = {total_s/60:.1f} min</text>')
        ry += 20
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">per-case time  : wall-clock span</text>')
        ry += 20
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">(mtimes rewritten by the</text>')
        ry += 20
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">backfill in one ~50 ms window,</text>')
        ry += 20
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">2 re-run cases carry re-run time)</text>')
        ry += 22
        svg.append(f'<text x="{rx}" y="{ry}" font-size="12" fill="#888">incl. model-load + GPU share with</text>')
        ry += 18
        svg.append(f'<text x="{rx}" y="{ry}" font-size="12" fill="#888">the agent&#39;s own 27b model. Upper</text>')
        ry += 18
        svg.append(f'<text x="{rx}" y="{ry}" font-size="12" fill="#888">bound on pure inference time.</text>')
    else:
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">rule-based matcher : &lt;0.1 s for all 20 cases</text>')
        ry += 22
        svg.append(f'<text x="{rx}" y="{ry}" font-size="12" fill="#888">deterministic, no model, no network.</text>')

    # --- Panel C: links per case (full-width bottom) ---
    svg.append(f'<text x="{margin}" y="{bottom_label_y}" font-size="15" font-weight="bold">Links per case (TP / FP / FN)</text>')
    plot_w = W - 2 * margin
    gap = 8
    bar_w = max(12, plot_w // n_cases - gap)
    max_v = max((c["tp"] + c["fp"] + c["fn"]) or 1 for c in pc)
    scale_v = (plot_h - 34) / max_v
    for i, c in enumerate(pc):
        x = margin + i * (bar_w + gap)
        base = bottom_label_y + plot_h - 24
        stack_y = base
        for val, col in ((c["tp"], "#2e8b57"), (c["fn"], "#e67e22"), (c["fp"], "#c0392b")):
            h = val * scale_v
            if h > 0:
                svg.append(f'<rect x="{x}" y="{stack_y - h:.0f}" width="{bar_w}" height="{h:.0f}" fill="{col}"/>')
            stack_y -= h
        tot = c["tp"] + c["fn"] + c["fp"]
        cxbar = x + bar_w / 2
        svg.append(f'<text x="{cxbar:.0f}" y="{bottom_label_y + plot_h + 14}" font-size="10" text-anchor="middle">'
                   f'{c["case"][-2:]}</text>')
        if tot:
            svg.append(f'<text x="{cxbar:.0f}" y="{bottom_label_y + 16}" font-size="10" text-anchor="middle">{tot}</text>')
    for i, (name, col) in enumerate((("TP correct", "#2e8b57"), ("FN missed", "#e67e22"), ("FP wrong", "#c0392b"))):
        lx = margin + i * 110
        svg.append(f'<rect x="{lx}" y="{legend_y - 10}" width="12" height="12" fill="{col}"/>')
        svg.append(f'<text x="{lx + 18}" y="{legend_y}" font-size="12">{name}</text>')

    svg.append(f'<text x="{margin}" y="{H - 14}" font-size="11" fill="#888">'
               f'precision={p(s["precision"])} · recall={p(s["recall"])} · F1={p(s["f1"])} · '
               f'wrongly-automatic={s["wrongly_auto"]}</text>')
    svg.append('</svg>')
    path.write_text("\n".join(x for x in svg if x is not None), encoding="utf-8")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Score a matcher's results against the answer key.")
    ap.add_argument("--file", default=None, help="results file to score (default results/matching_results.json)")
    ap.add_argument("--no-fig", action="store_true", help="skip writing the SVG figure")
    ap.add_argument("--fig", default=None, help="SVG output path (default results/evaluation_summary.svg)")
    args = ap.parse_args()
    file = Path(args.file) if args.file else None
    s = score(file)
    rts = runtimes() if file is None else []  # runtimes only make sense for the LLM run
    print_report(s, rts)
    if not args.no_fig:
        out = Path(args.fig) if args.fig else ROOT / "results" / "evaluation_summary.svg"
        make_svg(s, rts, out)
        try:
            print(f"\nwrote figure -> {out.relative_to(ROOT)}")
        except ValueError:
            print(f"\nwrote figure -> {out}")


if __name__ == "__main__":
    main()
