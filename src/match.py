"""Finding follow-through — Ollama-based report matching.

One model call per case. The model sees all of a case's records in exam order and
returns a JSON array: for each record, which EARLIER record (in the same exam or
before) it continues — or that nothing is earlier / it is new / a negative search.
The row schema (case_id, new_record, status, link, score, rule, fields) is chosen
to match the answer key's reading so the result table scores directly against
answer_key.json, which is a scoring reference only — never a matching input.

Usage:
    python3 src/match.py                # all cases
    python3 src/match.py SYN-01         # one case
    python3 src/match.py --dry          # print the prompt, no model call
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from records import load as load_records, load_config  # noqa: E402


def get_logger(root: Path | None = None) -> logging.Logger:
    """Configure + return the pipeline logger: file at logs/match.log (DEBUG),
    console at INFO. Calling it twice is safe (idempotent handler guard)."""
    log_root = root or (Path(__file__).resolve().parent.parent)
    log_dir = log_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger("fft.match")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    # ponytail: idempotent — guard so a second call (tests / reload) doesn't stack handlers
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(log_dir / "match.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(ch)
    return log


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def cfg_ollama() -> dict:
    base = Path(__file__).resolve().parent.parent
    c = {}
    if (base / "config" / "config.toml").is_file():
        with open(base / "config" / "config.toml", "rb") as f:
            c = tomllib.load(f).get("ollama", {})
    base_url = os.environ.get("OLLAMA_HOST") or c.get("base_url") or "http://localhost:11434"
    return {
        "base_url": re.sub(r"/*$", "", base_url.strip()),
        "model": os.environ.get("OLLAMA_MODEL") or c.get("model") or "medgemma:27b",
        "temperature": float(os.environ.get("OLLAMA_TEMPERATURE", c.get("temperature", 0))),
    }


def output_path(cli_out: str | None, root: Path) -> Path:
    if cli_out:
        return Path(cli_out).expanduser().resolve()
    try:
        out = load_config(root)["paths"].get("output", "results/matching_results.json")
    except Exception:
        out = "results/matching_results.json"
    return (root / out).resolve()


def cases_to_run(store, cli_ids: list[str] | None):
    if cli_ids:
        return [store.case(cid) for cid in cli_ids]
    return list(store.cases)


# --------------------------------------------------------------------------- #
# Ollama call (stdlib only)
# --------------------------------------------------------------------------- #
def _post(base_url: str, path: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ollama_complete(cfg: dict, system: str, prompt: str, timeout: int,
                    logger: logging.Logger | None = None) -> str:
    if logger:
        logger.debug("ollama POST model=%s base_url=%s timeout=%ss prompt_chars=%d",
                     cfg["model"], cfg["base_url"], timeout, len(prompt))
    d = _post(cfg["base_url"], "/api/chat", {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": cfg["temperature"]},
    }, timeout=timeout)
    content = d["message"].get("content") or d["message"].get("reasoning_content") or ""
    if isinstance(content, list):
        content = " ".join(str(p) for p in content)
    text = str(content).strip()
    if logger:
        logger.debug("ollama response chars=%d done_reason=%s", len(text), d.get("done_reason"))
        logger.debug("ollama raw response: %s", text[:400])
    return text


# --------------------------------------------------------------------------- #
# Record -> model-visible view
# --------------------------------------------------------------------------- #
def _fmt_location(loc: dict) -> str:
    parts = []
    for k in ("quadrant", "clock", "depth", "fn_cm", "extent", "view_basis"):
        if loc.get(k) is not None:
            parts.append(f"{k}={loc[k]}")
    if loc.get("stated_as"):
        parts.append(f'as stated: "{loc["stated_as"]}"')
    return "; ".join(p for p in parts if p) or "(no location)"


def record_view(rec: dict) -> str:
    out = [rec["finding_id"],
           f"modality={rec.get('modality')}",
           f"laterality={rec.get('laterality')}",
           f"site={rec.get('site')}",
           f"finding_type={rec.get('finding_type')}"]
    if rec.get("location"):
        out.append(f"location: {_fmt_location(rec['location'])}")
    if rec.get("size_mm") is not None:
        out.append(f"size_mm={rec['size_mm']}")
    if rec.get("descriptors"):
        out.append(f"descriptors: {rec['descriptors']}")
    status = rec.get("status_statement")
    if status:
        if isinstance(status, dict):
            out.append(f"status: {status.get('value')} ('{status.get('source_span')}')")
        else:
            out.append(f"status: {status}")
    for corr in rec.get("correspondence") or []:
        out.append(f"correspondence: {corr.get('polarity')} / {corr.get('certainty')} -> "
                   f"'{corr.get('target_text')}' ('{corr.get('source_span')}')")
    for k in ("assessment_literal", "recommendation_literal"):
        if rec.get(k):
            out.append(f"{k}: {rec[k]}")
    spec = rec.get("specimen")
    if spec:
        out.append(f"specimen: label={spec.get('specimen_label')} kind={spec.get('specimen_kind')} "
                   f"clip={spec.get('clip_statement')}")
    if rec.get("retrospective_note"):
        out.append(f"retrospective_note: {rec['retrospective_note']}")
    return " | ".join(out for out in out if out)  # ponytail: single pass, no separate filter


SYSTEM = (
    "You are a clinician assistant linking breast-imaging findings across one patient's "
    "exam sequence, in exam order (E1 then E2 then ...). One record per finding statement, "
    "laid out as a list. For the list you are given, decide for EACH record which EARLIER "
    "record (a record from an earlier exam, not itself, not a later one) it CONTINUES — the "
    "same lesion re-described — or whether it is NEW (no earlier record to attach to) or a "
    "NEGATIVE SEARCH ('no correlate') that attaches to an earlier finding it searched for. "
    "Evidence to weigh: laterality and site are hard gates (different breast, or "
    "axilla-vs-breast => no link); otherwise use quadrant, clock, depth, distance-from-"
    "nipple, extent, finding type, descriptors, size, and the stated correspondence/status "
    "text. A pathology specimen attaches by clip statement, location, or specimen label. "
    "A hedged statement ('may account for', 'likely the correlate') means the link is a "
    "CANDIDATE, not automatic. Return STRICT JSON: a JSON array with one object per input "
    "record, in the same order, exactly the object shape the user asks for. No prose, no "
    "markdown, no extra keys."
)


def _records_with_exam(case: dict) -> list[dict]:
    """Records in exam order, each annotated with its exam's header/indication/comparison modality so the model sees the full context (e.g. a header says LEFT but body says right)."""
    out = []
    for e in case.get("exams", []):
        header = e.get("header_laterality")
        if header and e.get("header_laterality") != e.get("exam_laterality"):
            header = f"{e.get('exam_laterality','?')} (header says {header!r} — CONFLICT)"
        else:
            header = e.get("header_laterality")
        for r in e.get("extracted_findings", []):
            out.append((r, e, header))
    return out


def _records(case: dict) -> list[dict]:
    return [r for e in case.get("exams", []) for r in e.get("extracted_findings", [])]


def build_prompt(case: dict, logger: logging.Logger | None = None) -> tuple[str, str]:
    recs = _records(case)
    if logger:
        logger.debug("build_prompt %s: %d records to match", case.get("case_id"), len(recs))
        for r in recs:
            loc = r.get("location") or {}
            loc_keys = [k for k in ("quadrant", "clock", "depth", "fn_cm", "extent", "view_basis")
                        if loc.get(k) is not None]
            extra = [k for k in ("size_mm", "descriptors", "status_statement",
                                "specimen", "retrospective_note") if r.get(k)]
            n_corr = len(r.get("correspondence") or [])
            if n_corr:
                extra.append(f"correspondence={n_corr}")
            logger.debug("  build_prompt %s: %s | %s%s",
                         case.get("case_id"), r.get("finding_id"),
                         loc_keys or "(no location fields)",
                         ("  +" + ",".join(extra)) if extra else "")
    lines = []
    for r, e, header in _records_with_exam(case):
        view = " | ".join([
            r["finding_id"],
            f"[exam {e.get('exam_id')} · {e.get('modality')} · {e.get('exam_type')}] "
            + (f"header_laterality='header: {header}' " if header else ""),
        ])
        lines.append(view + "\n  " + record_view(r))
    body = "\n".join(lines)
    prompt = (
        "CASE " + case.get("case_id", "?") + " — the records, in exam order (E1 then E2 then ...).\n"
        "The exam line for each record shows the exam modality, type, and (when present) a "
        "header_laterality that CONTRADICTS the body — treat a conflicting header as a REVIEW "
        "TRIGGER and NEVER apply the link automatically.\n\n"
        + body +
        "\n\nFor each record, above, in the same order, return a JSON array of THIS object "
        "(no prose, no markdown):"
        "\n{"
        ' "new_record": "<finding_id>",'
        ' "status": "automatic"|"candidate"|"none",'
        ' "link_id": "<an EARLIER finding_id, or null>",'
        ' "certainty": <0-100 integer>,'
        ' "reason": "<the decisive rule and the fields it read, <=30 words>"'
        "}"
        "\nstatus: automatic = this record continues an EARLIER record with high confidence and may be applied without physician review; "
        "candidate = a plausible continuation a person must confirm (use for hedged 'may/likely' statements, ties "
        "between two earlier records, or a negative search that attaches to an earlier record); "
        "none = this is a NEW finding (no earlier record to attach to), or a negative search with nothing to attach to.\n"
        "If link_id is null, status MUST be 'none'; if status is 'automatic' or 'candidate', link_id must be an earlier record.\n"
        "link_id: an EARLIER finding_id (a record from an earlier exam, before this one) that the new record continues; "
        "null if there is no earlier record to attach to. It must be one of the records listed ABOVE this one.\n"
        "score: 0-100, 100 = unambiguous."
    )
    return SYSTEM, prompt


# --------------------------------------------------------------------------- #
# Model output -> row
# --------------------------------------------------------------------------- #
def _to_int(x, lo=0, hi=100, default=50) -> int:
    try:
        v = int(float(str(x).strip().rstrip("%")))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


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


def _exam_context(case: dict) -> dict[str, list[str]]:
    """For each finding_id in the case, list the exam-level flags that should be carried into the row."""
    ctx: dict[str, list[str]] = {}
    for e in case.get("exams", []):
        flags = []
        if e.get("header_laterality") and e.get("header_laterality") != e.get("exam_laterality"):
            flags.append(f"header_laterality conflicts with body: header={e.get('header_laterality')!r} body={e.get('exam_laterality')!r}")
        if e.get("indication"):
            flags.append(f"indication={e.get('indication')!r}")
        for r in e.get("extracted_findings", []):
            ctx.setdefault(r.get("finding_id"), [])
            if flags:
                ctx[r["finding_id"]].extend(flags)
    return ctx


def _find_rec(recs: list[dict], fid: str):
    for r in recs:
        if r.get("finding_id") == fid:
            return r
    return None


def _is_earlier(cand_fid: str, new_fid: str) -> bool:
    def ek(fid: str) -> int:
        m = re.match(r"^SYN-\d+-E(\d+)-", fid)
        return int(m.group(1)) if m else 99
    return ek(cand_fid) < ek(new_fid)


def parse_response(case: dict, text: str, recs: list[dict],
                   logger: logging.Logger | None = None) -> list[dict]:
    """Model JSON string -> list of row dicts, one per record in exam order.

    Each row carries a `review_required` flag: True when the model status is
    'candidate', OR when the row's exam has a header_laterality / exam_laterality
    conflict (a review trigger the action plan expects the person to see).
    """
    logger = logger or logging.getLogger("fft.match")
    logger.debug("parse_response %s: %d records in, model_text_chars=%d",
                 case.get("case_id"), len(recs), len(text))
    m = re.search(r"\[[\s\S]*\]", text)
    arr = None
    if m:
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            arr = None
    by_new: dict[str, dict] = {}
    if isinstance(arr, list):
        for obj in arr:
            if isinstance(obj, dict) and obj.get("new_record"):
                by_new[str(obj["new_record"]).strip()] = obj
    exam_flags = _exam_context(case)

    def _row_for(rec: dict, obj: dict | None, model_error: str | None = None) -> dict:
        fid = rec["finding_id"]
        if logger:
            logger.debug("parse %s: %s model_status=%s model_link=%s",
                         case.get("case_id"), fid,
                         (obj or {}).get("status"), (obj or {}).get("link_id"))
        row = {
            "case_id": case["case_id"],
            "new_record": fid,
            "status": "none", "link": None, "certainty": 0,
            "rule": model_error or "", "fields": _fields_used(rec),
            "review_required": bool(exam_flags.get(fid)),
            "model_error": model_error,
        }
        if obj is None:
            return row
        status = str(obj.get("status", "none")).strip().lower()
        if status not in ("automatic", "candidate", "none"):
            status = "candidate" if "candidate" in status else "none"
        link = obj.get("link_id")
        if link:
            link = str(link).strip()
            if not _is_earlier(link, fid):
                cand = [r["finding_id"] for r in recs if r["finding_id"].endswith(link) and _is_earlier(r["finding_id"], fid)]
                link = cand[0] if len(cand) == 1 else None
            if _find_rec(recs, link) is None:
                link = None
        # invariant: no link => status must be 'none' (a first record is new, not automatic)
        if not link and status in ("automatic", "candidate"):
            status = "none"
        row["status"] = status
        row["rule"] = (str(obj.get("reason") or obj.get("rule") or "")).strip()
        row["certainty"] = _to_int(obj.get("certainty"))
        if status in ("automatic", "candidate") and link:
            row["link"] = link
        row["review_required"] = row["review_required"] or status == "candidate"
        return row

    if not isinstance(arr, list):
        return [_row_for(r, None, "model output not parsable: " + text[:200]) for r in recs]
    return [_row_for(rec, by_new.get(rec["finding_id"])) for rec in recs]


# --------------------------------------------------------------------------- #
# Drive cases
# --------------------------------------------------------------------------- #
def _write(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
        f.write("\n")


# ponytail: resumable run — one checkpoint JSON per case under <state>/<case>.json
def _state_path(case: dict, state_dir: Path) -> Path:
    return state_dir / f"{case['case_id']}.json"


def _load_case_state(case: dict, state_dir: Path) -> list[dict] | None:
    p = _state_path(case, state_dir)
    if p.is_file():
        try:
            with open(p) as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except json.JSONDecodeError:
            pass  # corrupt checkpoint: redo this case
    return None


def run_case(cfg: dict, case: dict, timeout: int, state_dir: Path,
             max_attempts: int = 5, logger: logging.Logger | None = None) -> list[dict]:
    """Run one case (or reuse its checkpoint). Returns the row list.

    Retries up to `max_attempts` on failure (GPU contention makes individual
    calls slow/unreliable here). `timeout` is the per-attempt READ budget in
    seconds (default 1800); connection is fast, the wait is in generation.
    """
    logger = logger or logging.getLogger("fft.match")
    cid = case.get("case_id")
    cached = _load_case_state(case, state_dir)
    if cached is not None:
        logger.info("[%s] checkpoint hit — reusing %d rows from %s",
                    cid, len(cached), _state_path(case, state_dir).name)
        return cached
    recs = _records(case)
    logger.info("[%s] building prompt (%d records across %d exams) ...",
                cid, len(recs), len(case.get("exams", [])))
    system, prompt = build_prompt(case, logger=logger)
    logger.debug("[%s] prompt: system=%d chars, user=%d chars",
                 cid, len(system), len(prompt))
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        logger.info("[%s] Ollama call, attempt %d/%d (timeout=%ss, model=%s) ...",
                    cid, attempt, max_attempts, timeout, cfg["model"])
        try:
            text = ollama_complete(cfg, system, prompt, timeout, logger=logger)
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            logger.warning("[%s] attempt %d/%d FAILED: %s", cid, attempt, max_attempts, repr(e))
            if attempt < max_attempts:
                continue
            logger.error("[%s] giving up after %d attempts: %s", cid, max_attempts, repr(last_err))
            raise SystemExit(f"Ollama call failed for {cid} after {max_attempts} attempts: {e!r}. "
                             f"Server: {cfg['base_url']}, model: {cfg['model']}. "
                             f"Case is not checkpointed; re-run to retry it.")
    else:  # pragma: no cover
        raise SystemExit(f"Ollama call failed for {cid}: {last_err!r}")
    logger.info("[%s] model returned %d chars — parsing", cid, len(text))
    rows = parse_response(case, text, recs, logger=logger)
    _write(rows, _state_path(case, state_dir))
    auto = sum(1 for r in rows if r["status"] == "automatic")
    cand = sum(1 for r in rows if r["status"] == "candidate")
    none = sum(1 for r in rows if r["status"] == "none")
    review = sum(1 for r in rows if r.get("review_required"))
    errs = sum(1 for r in rows if r["model_error"])
    logger.info("[%s] done: %d rows | automatic=%d candidate=%d none=%d review_required=%d errors=%d | checkpointed to %s",
                cid, len(rows), auto, cand, none, review, errs,
                _state_path(case, state_dir).name)
    return rows


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
        "model_errors": sum(1 for r in rows if r["model_error"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Ollama-based finding follow-through matcher (one call per case).")
    ap.add_argument("case_ids", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--out", default=None, help="output JSON path (default results/matching_results.json)")
    ap.add_argument("--timeout", type=int, default=1800, help="per-attempt read timeout (seconds). Default 1800 (30 min); the GPU is shared with the agent.")
    ap.add_argument("--state", default=None, help="checkpoint dir (default <out-dir>/cases). Re-run skips completed cases.")
    ap.add_argument("--debug", action="store_true", help="also stream DEBUG-level detail to the console (file always logs DEBUG)")
    ap.add_argument("--dry", action="store_true", help="print the prompt, do not call the model")
    args = ap.parse_args()

    store = load_records()
    root = store.root
    cases = cases_to_run(store, args.case_ids)
    log = get_logger(root)
    if args.debug:
        # flip the console handler to DEBUG for this run
        for h in log.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)
    log.info("pipeline start: %d case(s) selected%s", len(cases),
             f" -> {[c['case_id'] for c in cases]}" if len(cases) <= 5 else "")
    if store.reports:
        log.debug("records store: %d cases loaded, %d report sections from reports.md",
                  len(store.cases), len(store.reports))
    else:
        log.debug("records store: %d cases loaded (no reports.md attached)", len(store.cases))

    if args.dry:
        for case in cases:
            system, prompt = build_prompt(case, logger=log)
            log.info("DRY %s: system=%d chars, prompt=%d chars",
                     case["case_id"], len(system), len(prompt))
            print("\n".join(["-" * 72, "-- SYSTEM --", system, "-- PROMPT --", prompt, "-" * 72]) + "\n")
        log.info("pipeline end (dry run, no model calls)")
        return

    cfg = cfg_ollama()
    log.info("Ollama config: model=%s base_url=%s temperature=%s",
             cfg["model"], cfg["base_url"], cfg["temperature"])
    out_path = output_path(args.out, root)
    state_dir = Path(args.state) if args.state else (out_path.parent / "cases")
    state_dir.mkdir(parents=True, exist_ok=True)
    log.info("output: %s | checkpoints: %s", out_path, state_dir)

    rows: list[dict] = []
    for i, case in enumerate(cases, 1):
        n = sum(len(e.get("extracted_findings", [])) for e in case.get("exams", []))
        log.info("[%d/%d] ==== case %s (%d records) ====", i, len(cases), case["case_id"], n)
        case_rows = run_case(cfg, case, timeout=args.timeout, state_dir=state_dir, logger=log)
        rows.extend(case_rows)
    _write(rows, out_path)
    s = summarise(rows)
    log.info("pipeline end: %d row(s) total | automatic=%d candidate=%d none=%d "
             "review_required=%d model_errors=%d",
             s["total"], s["automatic_linked"], s["candidate_linked"],
             s["status_counts"].get("none", 0),
             sum(1 for r in rows if r.get("review_required")),
             s["model_errors"])
    log.info("wrote master table -> %s", out_path)
    # summary to stdout for humans, not just the log
    print("\n== summary ==")
    for k, v in s.items():
        print(f"  {k}: {v}")
    print(f"\nwrote: {out_path}")


if __name__ == "__main__":
    main()
