# Finding Follow-Through — Test Set (v0.2)

> **Staging folder** `MA_Staging/Testing/Finding_Follow_Through/`. Uncontrolled until the requirements it verifies are. Started 2026-10-23, v0.2 2026-10-27 — Rachel Orth.
>
> **What this is.** The synthetic test set for the finding follow-through function: 20 cases, 144 finding records, with ground truth. It verifies the candidates in `Requirements/MA_Finding-Follow-Through_Candidate-Requirements-and-Data-Model_v0.2.md`, which names the cases for each one. Entirely synthetic; no real patient data.
>
> **What this is not.** Not a specification of the MDE's extraction: the clean records are what a perfect extraction would deliver, built by hand so that the MA-side matching can be tested on its own. Not evidence yet — it becomes evidence when the results table comes back.

* [ ] Files

| File | What | Who opens it | 
|---|---|---| 
| `Synthetic_Finding_Cases_v0.2.xlsx` | One row per case: narrative, lesions and conclusions, what the case tests; a modality coverage tab | Everyone — the index |
| `reports.md` / `reports.json` | The report texts, per case and exam, as a radiologist or pathologist would have written them | The tester, to check records against source |
| `records_clean.json` | The extracted records: one per finding statement, in the FT-DM shape — `finding_id`, `exam_id`, `modality`, `laterality`, `site`, `finding_type`, `location{quadrant, clock, depth, fn_cm, extent, view_basis, stated_as}`, `size_mm`, `descriptors`, `status_statement`, `correspondence[]`, `assessment_literal`, `recommendation_literal`, `specimen` (pathology only), `retrospective_note` (rare); field syntax in `metadata.conventions` | The tester — this is the input |
| `answer_key.json` | Ground truth per case: lesion chains, non-lesions, candidate-only links, review triggers, traps, expected behavior | Closed until a results table exists |
| `FFT_Action-Plan_Christoph_v0.1.md` | How to run the test and what to report | The tester |
| `src/records.py` | Record store: loads `records_clean.json`, validates the FT-DM shape, attaches `reports.md` per case | The matchers |
| `src/match_llm.py` | LLM matcher (Ollama): one model call per case → one row per finding (`status`, `link`, `score`, `rule`, `review_required`) | The tester |
| `src/match_rules.py` | Deterministic rule-based matcher: hard gates (laterality/site/location) + scored rules, same row schema, no model, runs in <0.1 s | The tester, for comparison |
| `src/evaluate.py` | Scores either result table against `answer_key.json` (P/R/F1) and renders the summary figure | The tester |
| `config/config.toml` | Ollama base URL, model name, temperature; input/output paths (LLM matcher only) | The tester |
| `tests/test_records.py`, `tests/test_match.py` | Offline self-checks (no model calls) | The tester, `pytest tests/` |
| `results/matching_results.json` | LLM matcher result table, one row per finding of all 20 cases | The tester — the deliverable |
| `results/matching_rules.json` | Rule-based matcher result table, one row per finding of all 20 cases | The tester — the comparison |
| `logs/match_llm.log`, `logs/match_rules.log` | DEBUG-level pipeline traces (prompt fields, raw model response, per-row parse decisions) | When defending a score |

## Matching implementation (this folder)

Two reference matchers for the action plan's *Build* step, deliberately implemented on the same row schema so they score identically and can be compared head-to-head. Both read `data/records_clean.json`, decide — for each case, in exam order — which *earlier* record each finding continues, and write one row per finding. The answer key is never part of either matcher: it is a scoring reference only.

**LLM matcher** (`src/match_llm.py`, Ollama, default `medgemma:27b` in `config/config.toml`). One call per case: the finding lines (FT-DM fields verbatim, plus the exam's `header_laterality`, flagged `CONFLICT` when it disagrees with the body) go into a single prompt. The model's strict-JSON response is parsed, then **invariants enforced by code**: no link ⇒ `status none`; link must be an *earlier* record of the *same* case; header/body conflict forces `review_required` and demotes `automatic → candidate`; unparseable output degrades to safe `none` rows. Each case is checkpointed to `results/cases/<case>.json`, so a re-run only redoes the cases that failed.

**Rule-based matcher** (`src/match_rules.py`). Deterministic, no model, <0.1 s for all 20 cases. **Exam-adjacency rule** (FT): a finding continues only from the immediately preceding exam — a lesion is never linked across a skipped exam; what may travel on is the already-matched lesion chain, not raw candidate records. Hard gates: same laterality, same site (breast-vs-axilla), compatible location (quadrant overlap / clock-within-one-hour-or-range-containment / depth within one third / distance-from-nipple within a per-modality-pair tolerance), and finding-type compatibility (incl. the documented evolutions asymmetry→mass, calcifications→non-mass-enhancement). Then scored rules: clock confirm, depth match, fn≤1 cm, same type, correspondence (affirmed +25 / hedged +15, never automatic), clip statement (pathology, strongest carrier), negative-search attach. A negated correspondence or `status: new` short-circuits to `none` (the SYN-03 trap). Ties break toward the most-recent exam / finding (the immediately-preceding-lesion reading). A header/body conflict demotes `automatic → candidate` and sets `review_required`, exactly as the LLM path. `--conservative` pins every link to `candidate` for the action plan's FT-REQ-24 reading ("nothing automatic until this stage measures it").


### Matching rules (rule-based matcher, as implemented in `src/match_rules.py`)

The decision procedure, in the exact order it runs. Every step gates the next; a finding that fails a hard gate is not a candidate at all.

**Step 0 — Short-circuits (immediate `none`, no candidates considered):**

- `correspondence` polarity is `negated` on any entry → **none** (SYN-03 trap: "does not correspond to…")
- `status_statement.value == "new"` → **none** (the record itself declares it is new)

**Step 1 — Candidate scan (hard gates, all must pass for a record to be a candidate):**

| Gate | Rule |
|---|---|
| Exam rank | Only records from exams **before** the new record's exam (never the same exam) |
| Laterality | `rec_new.laterality == rec_ref.laterality` (structural — cross-breast links are impossible, SYN-05) |
| Site | `rec_new.site == rec_ref.site` (breast vs. axilla, SYN-15) |
| Quadrant | Both have a quadrant → hour-ranges must **overlap** on the 12-h circle (boundary hours 12/3/6/9 count as shared with both adjacent quadrants); one missing → no constraint from this gate |
| Clock | Both have a clock value → point vs. range: point must be **inside** the range; point vs. point: angular difference ≤ `CLOCK_TOL_H`; boundary hour matches both adjacent quadrants |
| Depth | Both have a depth → same depth-third (anterior/middle/posterior); ABUS depth-from-skin → excluded (no constraint) |
| Distance from nipple | Both have a numeric value → `|fn_a − fn_b|` ≤ per-modality-pair tolerance (cm) |
| Finding type | `rec_new.type == rec_ref.type`, **or** a documented evolution: asymmetry→mass, calcifications→non-mass-enhancement |

**Distance-from-nipple tolerances (cm), keyed by modality pair:**

| Pair | cm | Pair | cm |
|---|---|---|---|
| MG ↔ US | **2.0** | US ↔ MRI | **3.0** |
| MG ↔ MRI | **3.0** | ABUS ↔ ABUS | **1.5** |
| all other pairs | **2.5** | | |

**Step 2 — Exam-adjacency restriction:**

If there are ≥ 1 candidates from the **immediately preceding exam** (`rank = new_exam − 1`), candidates are **restricted to that exam only**. Fallback to the full earlier history is allowed **only** when the previous exam has no same-lesion candidate (the SYN-07 pattern: the lesion skips a mammogram that reported nothing). This is what prevents E3→E1 skips.

**Step 3 — Scoring (base 50 + bonuses, applied only if ≥ 1 candidate survived):**

| Bonus | Condition | Points |
|---|---|---|
| Clock confirm | clock passed the hard gate (point-in-range, within tolerance, or boundary) | **+15** |
| Depth match | same depth-third | +5 |
| Small distance difference | `|fn_a − fn_b| ≤ 1.0 cm` | +5 |
| Type equal | `rec_new.finding_type == rec_ref.finding_type` (not just an evolution) | +5 |
| Correspondence — affirmed, not hedged | polarity `affirmed` and certainty ≠ `hedged` | **+25** |
| Correspondence — affirmed but hedged | polarity `affirmed` and certainty `hedged` (or "may"/"likely"/"probably" in text) | +15 |
| Clip statement present | `finding_type == "specimen"` and `specimen.clip_statement` is set (strongest carrier, pathology) | **+25** |
| Negative-search attach | `finding_type == "no correlate"` | +20 |

**Step 4 — Status assignment (highest score wins, tie-break: most-recent exam, then most-recent finding):**

| Condition | Result |
|---|---|
| `best_score ≥ 50` AND `best_score < 80` | `candidate` |
| `best_score ≥ 80` — but hedged correspondence, OR tie (margin < 15 pts to runner-up) | `candidate` + `review_required` |
| `best_score ≥ 80` AND not hedged AND no tie | `automatic` (or `candidate` in `--conservative` mode) |
| `best_score < 50` | `none` (row carries no link) |

**Step 5 — Header/body laterality conflict (evaluated after status is set, per-exam):**

If the new record's exam has `header_laterality ≠ exam_laterality` (the header says R, the body describes L, or vice versa) **and** a link was produced:
- `automatic` → **demoted to `candidate`**
- `review_required` → **set to `true`**

This is the SYN-19 trap: the body describes a left-bias but the header is "right."

**Step 6 — Review triggers (all of the following force `review_required = true` on the row):**

- Status is `candidate` (any cause: score in [50, 80), hedged, or tie)
- Hedged correspondence (any score)
- Tie with runner-up (margin < 15 pts)
- Header/body laterality conflict (Step 5)

**`--conservative` mode** (FT-REQ-24 reading): Pins all links to `candidate` regardless of score. "Nothing automatic until this stage has measured the system."

**Status vocabulary** (shared by both matchers, chosen to score 1:1 against the answer key): `automatic` = high confidence, applies without a person · `candidate` = plausible continuation a person must confirm (hedged "may/likely", ties, negative searches, any score < 80) · `none` = new lesion / chain-root / nothing to attach to. A `review_required` flag marks the rows the action plan's "review triggers" call out.

> **Note on LLM matcher:** The LLM path (`src/match_llm.py`) does **not** have the exam-adjacency restriction — the model can link across exams. The invariants enforced by code (no link ⇒ `none`; link must be earlier; header conflict ⇒ demotion) apply to both matchers. The rule-based matcher is the one with the full constraint stack above.

### Quick start

```bash
# LLM matcher (needs Ollama on localhost:11434, medgemma:27b pulled)
python3 src/match_llm.py SYN-01 --dry      # watch the prompt, no model call
python3 src/match_llm.py SYN-01            # one case -> results/cases/SYN-01.json
python3 src/match_llm.py                   # all 20 -> results/matching_results.json
python3 src/match_llm.py                   # re-run: skips done cases, retries failed

# Rule-based matcher (no model, instant)
python3 src/match_rules.py                 # all 20 -> results/matching_rules.json
python3 src/match_rules.py --conservative  # FT-REQ-24 reading: every link = candidate
python3 src/match_rules.py --score         # also P/R/F1 vs answer_key

# Score either table + render the figure
python3 src/evaluate.py --file results/matching_results.json --fig results/evaluation_llm.svg
python3 src/evaluate.py --file results/matching_rules.json   --fig results/evaluation_rules.svg

# Tests (offline, no model calls)
python3 -m pytest tests/ -v
```

Everything logs to `logs/match_llm.log` / `logs/match_rules.log` (DEBUG) and the console (INFO). To defend a row, find it in `results/cases/<case>.json` (LLM) or `results/cases_rules/<case>.json` (rule-based), then `grep <finding_id> logs/` for the prompt fields / rule reasons, the raw model response (LLM only), and the per-row decision.

## Reading the records

Each report paragraph that describes a finding is one record, literally as stated, no inference. Combined reports (MG+US, CEM low-energy + recombined) produce one record per modality section. A negative search ("no sonographic correlate") is a record with `finding_type: "no correlate"` and a negated correspondence; it is never a finding. A pathology report produces one record per specimen part. Conventions (clock mapping per breast, boundary clocks, depth and distance by modality, the vocabularies) are in `records_clean.json` → `metadata.conventions`.

## Reading the answer key

* **lesions** — the chains a correct matcher reconstructs, finding IDs in exam order, with the lesion's conclusion in the Data Model vocabulary (Benign requires tissue; imaging-only endings are Negative).
* **non_lesions** — screening findings resolved as not-a-lesion; their records chain to each other and to nothing else.
* **candidate_links** — links that must be proposed and never applied automatically (hedged correspondence). A confirmed link is correct; an automatic one is a failure. One record (SYN-17-E1-F2) sits only here, with no chain: correct if proposed or left unlinked, wrong if linked automatically.
* **review_triggers** — what must reach a person (laterality conflicts, ties, hedges).
* **traps** and **expected_behavior** — why the case is in the set.

## Layers

1. **Clean** (this folder): the records as a perfect MDE would deliver them. Measures the matching logic alone.
2. **Degraded** (to follow, after the clean results): fields removed, locations vaguer, OCR-style errors, header/body conflicts. Measures whether matching fails safely.
3. **MDE extraction** (to follow): the same 20 cases from report text, then a separate fax identity set with real document images. Measures extraction, not matching.

## Prior work

The first exploration (mid-October 2026) — a 3-row sample of a public, English-translated mammography report dataset and Christoph's first per-lesion location sheet over it — is superseded by this set and archived locally (`_archive/MA_findings linking …`). Two things from it carry forward: the public-dataset sample is a candidate real-text source for the stage 2 extraction test (all-caps, non-ACR vocabulary such as "nodular image" and "pectoaxillary region"; source and license to be recorded before reuse), and its first pass recorded one lesion where the report described three nodules, the specimen-split and finding-type problem in miniature. Edwin's ACM card mockup for a multi-finding case, which was in the same folder, is in `Design_Notes/`.

## Change notes

*v0.2 — 2026-10-27:* nine cases reworked for modality coverage (screening MRI, problem-solving and pre-op MRI, MRI-guided biopsy, screening and diagnostic CEM, ABUS and screening ultrasound); records regenerated in the FT-DM shape; conclusion vocabulary aligned to the Data Model; answer key split from the records. *v0.1 — 2026-10-23:* 20 narratives, MG → US throughout.