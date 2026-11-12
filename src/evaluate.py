#!/usr/bin/env python3
"""Score a matcher's results against answer_key.json and emit the action-plan
results table + report (§4 Results table / Report, FFT_Action-Plan_Christoph_v0.1.md).

Outputs per matcher (results_dir/):
  evaluation_<name>.json   full structured report for programmatic use
  evaluation_<name>.csv    the flat results table (one row per proposal/miss)
  evaluation_<name>.svg    status donut + runtime (upper), links-per-case (lower)

Run:  python3 src/evaluate.py [--out results/]   (scores both matchers by default)
      python3 src/evaluate.py --file results/matching_rules.json --name rules
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = ROOT / "results" / "answer_key.json"
RESULTS_DIR_DEFAULT = ROOT / "results"

# --------------------------------------------------------------------------- #
# GT helpers
# --------------------------------------------------------------------------- #
def _load_key() -> list[dict]:
    return json.loads(KEY.read_text())["keys"]


def _gt_edges_by_case(keys: list[dict]) -> dict[str, set[tuple[str, str]]]:
    """(new, prev) edges from lesions + non_lesions + candidate_links, by case_id."""
    out: dict[str, set[tuple[str, str]]] = {}
    for k in keys:
        cid = k["case_id"]
        edges: set[tuple[str, str]] = set()
        for chain_key in ("lesions", "non_lesions", ):
            for chain in (k.get(chain_key) or []):
                ids = list(chain.get("finding_ids") or [])
                for prev, new in zip(ids, ids[1:]):
                    edges.add((new, prev))
        for cl in (k.get("candidate_links") or []):
            ids = list(cl.get("finding_ids") or [])
            for prev, new in zip(ids, ids[1:]):
                edges.add((new, prev))
        out[cid] = edges
    return out


def _cand_only_by_case(keys: list[dict]) -> dict[str, set[str]]:
    """Findings that appear ONLY as the 'new' side of a candidate_link.
    Unlinked = acceptable; automatic = wrongly-automatic (BC-008)."""
    out: dict[str, set[str]] = {}
    for k in keys:
        s: set[str] = set()
        for cl in (k.get("candidate_links") or []):
            ids = list(cl.get("finding_ids") or [])
            if len(ids) >= 2:
                s.add(ids[1])
        out[k["case_id"]] = s
    return out


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_rows(rows: list[dict], gt_edges: dict[str, set[tuple[str, str]]],
               cand_only: dict[str, set[str]]) -> dict:
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)

    total_tp = total_fp = total_fn = total_wa = 0
    per_case_out: list[dict] = []
    table_rows: list[dict] = []          # flat action-plan table
    rule_stats: dict[str, dict] = {}     # rule -> {fired, correct}
    field_stats: dict[str, int] = {}     # field -> count as load-bearing (TP rows)

    for cid, case_rows in sorted(by_case.items()):
        gt = gt_edges.get(cid, set())
        cand_only_set = cand_only.get(cid, set())
        matched: set[tuple[str, str]] = set()
        tp = fp = fn = wa = 0

        for r in case_rows:
            link = r.get("link")
            status = r.get("status", "none")
            rule = r.get("rule", "")
            fields = r.get("fields") or []
            outcome: str
            correct_link: str | None = None

            if link:
                edge = (r["new_record"], link)
                if edge in gt:
                    outcome = "correct"
                    tp += 1
                    matched.add(edge)
                    correct_link = link
                else:
                    outcome = "wrong link"
                    fp += 1
                    # find the actual correct link for display
                    for e in gt:
                        if e[0] == r["new_record"]:
                            correct_link = e[1]
                            break
                if status == "automatic" and r["new_record"] in cand_only_set:
                    outcome = "wrongly automatic"
                    wa += 1
            else:
                outcome = "missed" if any(e[0] == r["new_record"] for e in gt) else "ok"

            table_rows.append({
                "case": cid,
                "new_record": r["new_record"],
                "proposed_continuation": link or "",
                "correct_continuation": correct_link or "",
                "status": status,
                "rule_fired": rule,
                "fields_read": "; ".join(fields) if isinstance(fields, list) else str(fields),
                "score": r.get("score", r.get("certainty", "")),
                "outcome": outcome,
            })

            # rule statistics
            if rule:
                key = re.sub(r"^RULE-\w+: ", "", rule).split(" + ")[0].strip()
                if key not in rule_stats:
                    rule_stats[key] = {"fired": 0, "correct": 0}
                rule_stats[key]["fired"] += 1
                if outcome == "correct":
                    rule_stats[key]["correct"] += 1

            # field load-bearing count (only on TP rows)
            if outcome == "correct":
                for f in (fields if isinstance(fields, list) else [str(fields)]):
                    field_stats[f] = field_stats.get(f, 0) + 1

        # FN: GT edges not matched
        fn = len([e for e in gt if e not in matched])
        total_tp += tp
        total_fp += fp
        total_fn += fn
        total_wa += wa
        per_case_out.append({
            "case": cid, "n_rows": len(case_rows),
            "tp": tp, "fp": fp, "fn": fn, "wa": wa,
            "auto": sum(1 for r in case_rows if r["status"] == "automatic"),
            "cand": sum(1 for r in case_rows if r["status"] == "candidate"),
            "none": sum(1 for r in case_rows if r["status"] == "none"),
            "review": sum(1 for r in case_rows if r.get("review_required")),
            "errors": sum(1 for r in case_rows if r.get("model_error")),
        })

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "rows": len(rows),
        "tp": total_tp, "fp": total_fp, "fn": total_fn, "wa": total_wa,
        "precision": precision, "recall": recall, "f1": f1,
        "status_distribution": {
            "automatic": sum(1 for r in rows if r["status"] == "automatic"),
            "candidate": sum(1 for r in rows if r["status"] == "candidate"),
            "none": sum(1 for r in rows if r["status"] == "none"),
        },
        "review_required": sum(1 for r in rows if r.get("review_required")),
        "model_errors": sum(1 for r in rows if r.get("model_error")),
        "per_case": per_case_out,
        "table": table_rows,
        "rule_stats": rule_stats,
        "field_load_bearing": dict(sorted(field_stats.items(), key=lambda x: -x[1])),
    }


# --------------------------------------------------------------------------- #
# Report generation
# --------------------------------------------------------------------------- #
def report(s: dict, name: str) -> str:
    L: list[str] = []
    a = L.append
    a(f"# Matching results — {name}")
    a("")
    a("## 1. Precision and recall")
    a("")
    auto = s["status_distribution"]["automatic"]
    cand = s["status_distribution"]["candidate"]
    none = s["status_distribution"]["none"]
    a(f"| Metric | {name} |")
    a(f"|---|---|")
    a(f"| Rows scored | {s['rows']} |")
    a(f"| True links (TP) | {s['tp']} |")
    a(f"| Wrong links (FP) | {s['fp']} |")
    a(f"| Missed links (FN) | {s['fn']} |")
    a(f"| Wrongly automatic (BC-008) | {s['wa']} |")
    a(f"| **Precision (all links)** | **{s['precision']:.3f}** |")
    a(f"| **Recall (all links)** | **{s['recall']:.3f}** |")
    a(f"| **F1** | **{s['f1']:.3f}** |")
    a(f"| Status distribution | auto={auto} · candidate={cand} · none={none} |")
    a(f"| Review required | {s['review_required']} |")
    a(f"| Model errors | {s['model_errors']} |")
    a("")

    a("## 2. Per-rule statistics")
    a("")
    rs = s.get("rule_stats", {})
    if rs:
        a(f"| Rule | Fired | Correct | Error rate |")
        a(f"|---|---|---|---|")
        for rule, d in sorted(rs.items(), key=lambda x: -x[1]["fired"]):
            err = d["fired"] - d["correct"]
            a(f"| {rule} | {d['fired']} | {d['correct']} | {err}/{d['fired']} |")
    else:
        a("_(No structured rules recorded — LLM reason strings vary per case.)_")
    a("")

    a("## 3. Field load-bearing analysis (evidence for stage 2 extraction scope)")
    a("")
    a("Fields that appear in correct (TP) proposals. Fields with 0 TP rows were never load-bearing.")
    a("")
    fs = s.get("field_load_bearing", {})
    a(f"| Field | Load-bearing count (TP rows) |")
    a(f"|---|---|")
    for field, count in fs.items():
        a(f"| `{field}` | {count} |")
    a("")
    a("> **Interpretation:** a field that appears in 0+ TP rows is *necessary* for at least one correct link.")
    a("> A field with 0 rows across all TPs can be dropped from stage 2's extraction scope without")
    a("> affecting matching correctness on this test set.")
    a("")

    a("## 4. Per-case breakdown")
    a("")
    a(f"| case | rows | auto | cand | none | review | TP | FP | FN | WA |")
    a(f"|---|---|---|---|---|---|---|---|---|---|")
    for c in s["per_case"]:
        a(f"| {c['case']} | {c['n_rows']} | {c['auto']} | {c['cand']} | {c['none']} "
          f"| {c['review']} | {c['tp']} | {c['fp']} | {c['fn']} | {c['wa']} |")
    a("")
    return "\n".join(L)


def write_csv(table_rows: list[dict], path: Path) -> None:
    if not table_rows:
        return
    cols = ["case", "new_record", "proposed_continuation", "correct_continuation",
            "status", "rule_fired", "fields_read", "score", "outcome"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in table_rows:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# SVG figure (unchanged from before)
# --------------------------------------------------------------------------- #
def make_svg(s: dict, path: Path, name: str = "") -> None:
    import math
    W = 900
    margin = 60
    p = lambda x: f"{x:.3f}"
    pc = s["per_case"]
    n_cases = len(pc)
    half0 = 90
    panel_label_y = 112
    top_block_y = half0 + 150
    top_block_h = 270
    top_block_bottom = half0 + 40 + top_block_h
    bottom_label_y = top_block_bottom + 34
    plot_h = 250
    legend_y = bottom_label_y + plot_h + 22
    H = legend_y + 42
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'font-family="DejaVu Sans,Arial,sans-serif" font-size="13">']
    svg.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    title = f"Finding Follow-Through — {name}" if name else "Finding Follow-Through — Matching Results"
    svg.append(f'<text x="{margin}" y="34" font-size="20" font-weight="bold">{title}</text>')
    svg.append(f'<text x="{margin}" y="56" font-size="12" fill="#555">'
               f'{s["rows"]} findings · {s["tp"]} TP / {s["fp"]} FP / {s["fn"]} FN · '
               f'P={p(s["precision"])} R={p(s["recall"])} F1={p(s["f1"])} · errors={s["model_errors"]}</text>')
    mid = W // 2
    svg.append(f'<line x1="{margin}" y1="{half0 + 44}" x2="{W - margin}" y2="{half0 + 44}" stroke="#e6e6e6"/>')
    # donut
    svg.append(f'<text x="{margin}" y="{panel_label_y}" font-size="15" font-weight="bold">Status distribution</text>')
    st = s["status_distribution"]
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
        lxx = cx + (r0 + 22) * math.cos(m2); lyy = cy0 + (r0 + 22) * math.sin(m2)
        anchor = "start" if math.cos(m2) >= 0 else "end"
        svg.append(f'<text x="{lxx:.1f}" y="{lyy:.1f}" font-size="12" text-anchor="{anchor}">{key} ({frac*100:.0f}%)</text>')
        ang = a1
    svg.append(f'<circle cx="{cx}" cy="{cy0}" r="{r0-34}" fill="white"/>')
    svg.append(f'<text x="{cx}" y="{cy0 - 6}" font-size="14" font-weight="bold" text-anchor="middle">{total}</text>')
    svg.append(f'<text x="{cx}" y="{cy0 + 10}" font-size="11" fill="#555" text-anchor="middle">findings</text>')
    ny = cy0 + r0 + 40
    svg.append(f'<text x="{margin}" y="{ny}" font-size="12" fill="#555">review_required: {s["review_required"]}</text>')
    svg.append(f'<text x="{margin}" y="{ny + 18}" font-size="12" fill="#555">model errors: {s["model_errors"]}</text>')
    # runtime panel
    rx = mid + 20
    svg.append(f'<text x="{rx}" y="{panel_label_y}" font-size="15" font-weight="bold">Runtime</text>')
    ry = panel_label_y + 30
    if name == "rules":
        svg.append(f'<text x="{rx}" y="{ry}" font-size="13">deterministic · &lt;0.1 s for all 20 cases</text>')
        ry += 22
        svg.append(f'<text x="{rx}" y="{ry}" font-size="12" fill="#888">no model, no network. Fully reproducible.</text>')
    else:
        from pathlib import Path as _P
        cases_dir = ROOT / "results" / "cases"
        files = list(cases_dir.glob("*.json")) if cases_dir.is_dir() else []
        if files:
            import os
            t = sorted(f.stat().st_mtime for f in files)
            span = t[-1] - t[0]
            svg.append(f'<text x="{rx}" y="{ry}" font-size="13">20 cases · {span:.0f} s ({span/60:.1f} min)</text>')
        else:
            svg.append(f'<text x="{rx}" y="{ry}" font-size="13">see logs for runtime</text>')
    # per-case bars
    svg.append(f'<text x="{margin}" y="{bottom_label_y}" font-size="15" font-weight="bold">Links per case (TP / FN / FP)</text>')
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
        svg.append(f'<text x="{cxbar:.0f}" y="{bottom_label_y + plot_h + 14}" font-size="10" '
                   f'text-anchor="middle">{c["case"][-2:]}</text>')
        if tot:
            svg.append(f'<text x="{cxbar:.0f}" y="{bottom_label_y + 16}" font-size="10" '
                       f'text-anchor="middle">{tot}</text>')
    for i, (n_, c_) in enumerate((("TP", "#2e8b57"), ("FN", "#e67e22"), ("FP", "#c0392b"))):
        lx = margin + i * 80
        svg.append(f'<rect x="{lx}" y="{legend_y - 10}" width="12" height="12" fill="{c_}"/>')
        svg.append(f'<text x="{lx + 18}" y="{legend_y}" font-size="12">{n_}</text>')
    svg.append(f'<text x="{margin}" y="{H - 14}" font-size="11" fill="#888">'
               f'P={p(s["precision"])} · R={p(s["recall"])} · F1={p(s["f1"])} · WA={s["wa"]}</text>')
    svg.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(svg), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def evaluate_one(file: Path, name: str, out_dir: Path) -> dict:
    keys = _load_key()
    gt_edges = _gt_edges_by_case(keys)
    cand_only = _cand_only_by_case(keys)
    rows = json.loads(file.read_text())
    s = score_rows(rows, gt_edges, cand_only)
    # write report markdown
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_path = out_dir / f"evaluation_{name}.md"
    rep_path.write_text(report(s, name), encoding="utf-8")
    # write CSV
    csv_path = out_dir / f"evaluation_{name}.csv"
    write_csv(s["table"], csv_path)
    # write JSON (structured report)
    json_path = out_dir / f"evaluation_{name}.json"
    json_path.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    # write SVG
    svg_path = out_dir / f"evaluation_{name}.svg"
    make_svg(s, svg_path, name=name)
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description="Score matcher results against the answer key.")
    ap.add_argument("--file", default=None, help="single results file (default: auto-detect both)")
    ap.add_argument("--name", default=None, help="name for this evaluation (default: derived from filename)")
    ap.add_argument("--out", default=None, help="output directory (default results/)")
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else RESULTS_DIR_DEFAULT

    if args.file:
        f = Path(args.file)
        nm = args.name or f.stem.replace("matching_", "").replace(".json", "")
        if nm == "results":
            nm = "llm"
        s = evaluate_one(f, nm, out_dir)
    else:
        # auto-detect both
        pairs = []
        for f in sorted(out_dir.glob("matching_*.json")):
            # matching_results.json -> llm, matching_rules.json -> rules
            nm = f.stem.replace("matching_", "").replace(".json", "")
            if nm == "results":
                nm = "llm"
            pairs.append((f, nm))
        if not pairs:
            print("No results files found in", out_dir, file=sys.stderr)
            sys.exit(1)
        for f, nm in pairs:
            print(f"== Evaluating {f.name} (name={nm}) ==")
            s = evaluate_one(f, nm, out_dir)
            print(f"   TP={s['tp']} FP={s['fp']} FN={s['fn']} WA={s['wa']}  "
                  f"P={s['precision']:.3f} R={s['recall']:.3f} F1={s['f1']:.3f}")
            print(f"   -> {out_dir / ('evaluation_' + nm + '.md')}")
            print(f"   -> {out_dir / ('evaluation_' + nm + '.csv')}")
            print(f"   -> {out_dir / ('evaluation_' + nm + '.json')}")
            print(f"   -> {out_dir / ('evaluation_' + nm + '.svg')}")
            print()
    # side-by-side summary if both exist
    both = []
    for nm in ("llm", "rules"):
        p = out_dir / f"evaluation_{nm}.json"
        if p.is_file():
            both.append((nm, json.loads(p.read_text())))
    if len(both) == 2:
        print("== Comparison ==")
        print(f"{'':12s} {'LLM':>8s} {'Rules':>8s}")
        l, r = (b[1] for b in both)
        for metric in ("rows", "tp", "fp", "fn", "wa"):
            print(f"{metric:>11s}: {l[metric]:>8d} {r[metric]:>8d}")
        for metric in ("precision", "recall", "f1"):
            print(f"{metric:>11s}: {l[metric]:>8.3f} {r[metric]:>8.3f}")
        for nm, s in both:
            st = s["status_distribution"]
            print(f"  {nm:6s} status: auto={st['automatic']} cand={st['candidate']} none={st['none']}  "
                  f"review={s['review_required']}  errors={s['model_errors']}")


if __name__ == "__main__":
    main()
