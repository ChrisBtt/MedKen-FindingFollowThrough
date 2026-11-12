# Finding Follow-Through — Action Plan for Christoph (v0.1)

> **Working document — `MA_Staging/Testing/Finding_Follow_Through/`.** Uncontrolled. 2026-10-27 — Rachel Orth.
>
> **What this is.** The plan for the finding follow-through feasibility exploration: what has been decided, what is being tested, in what order, and what comes back. It is written to be read with `Requirements/MA_Finding-Follow-Through_Candidate-Requirements-and-Data-Model_v0.2.md` (the candidates and their field definitions) and the test set in this folder. Where it points at the fence — MA-BC-008, MA-BC-011 in the Requirements Document v2.7; R-029 and 4.5 in the Data Model v1.9; DC-13 and DC-14 in the Determination — read those directly.
>
> **Status of everything here.** Exploration. Nothing in the candidate file, this plan, or the test set changes a controlled or working-draft document. Where the exploration shows a candidate is executable and needed, that candidate goes to the Requirements Document or Data Model through its normal route (for several of them, a BC-008 or BC-011 restatement under determination trigger 20 or 22 first). Where it shows a candidate is not needed, it is dropped with a reason.

---

## 1. The job in one paragraph

A practice's reports describe the same lesion several times — screening mammogram, diagnostic mammogram, ultrasound, sometimes MRI or CEM, then one or more pathology reports — each in its own words and its own location notation. Finding follow-through is MA's function that proposes which earlier finding a new report's finding continues, so that each lesion carries its own recommendation, trajectory and conclusion. The Medken Data Engine (MDE) extracts the descriptors from report text; MA matches on them by a stated, deterministic rule and shows the rule, the fields and their source spans; a person confirms anything the rule can't settle on coded or literal fields. The MDE never links; MA never extracts. Everything below sits inside that split.

## 2. What has been added to the three descriptors, and why

The fence as written extracts **laterality, location, finding type** and nothing else. The test set shows that location alone can't solve the cases that matter, and the following inputs were added as **working assumptions for the exploration** — each with the case that breaks without it. They are candidates (FT-REQ / FT-DM in the candidate file), not decisions.

| Input | Where it sits in a report | Use case (fails without it) | Candidate |
|---|---|---|---|
| **Stated correspondence** — the radiologist's own sentence that a finding is, or is not, the same as another: "corresponds to the mammographic mass", "separate from the screening finding", "no sonographic correlate" | A clause in the Findings section following the finding it describes; sometimes only implicit through the Indication line and the Impression | SYN-03: an incidental mass at 11:00, 1 cm FN sits inside the resolved screening asymmetry's quadrant; location says link, the sentence says don't. SYN-10: "no sonographic correlate" at 10:00 must not become a phantom ultrasound finding. SYN-11: two masses an hour apart; the words break the tie | FT-REQ-12, 13, 14; FT-DM-03. **The target is the radiologist's words, not an ID**: the MDE delivers "the mammographic mass"; MA resolves which record that is |
| **Hedge** — stated vs hedged certainty: "likely", "may account for", "probably represents" | A word inside the correspondence clause | SYN-17: "may account for the screening asymmetry". Flattened to plain correspondence, MA would auto-link a new lesion into an old one's record. SYN-20: second-look "likely the correlate" | FT-REQ-15; `certainty` on FT-DM-03. Two values only; never a precision scale |
| **Change over time** — persists, stable, increased, resolved, no longer visualized, new since prior | Findings section, tied to the Comparison line | SYN-14: distance drifts 4 → 4.5 → 4 cm and size 8 → 9 → 13 mm; a tolerance loose enough to hold the chain would merge SYN-06's two masses; "increased (previously 9 mm)" is what holds it. SYN-05: "no longer visualized" closes the chain | FT-REQ-17; FT-DM-04. **Matching input only** — never sets a conclusion, clock or state (R-005 sets conclusions) |
| **Extent** — a finding stated as a region: "from 1:00 to 2:00", "8 to 11 cm", "2.5 cm extent" | The location statement | SYN-11: biopsy at 1:30 must match calcifications at 1:00–2:00. SYN-04: a negative search over 8–11 cm must attach to a distortion at 10 cm | FT-REQ-08; `extent` in FT-DM-01. Required |
| **Scalar size** — 9 mm, 22 mm | After the descriptors | Confirms nothing location hasn't already settled; size changes legitimately within one lesion and differs by modality (16 mm US / 18 mm MRI, SYN-20) | FT-DM-05: extracted for display; **tie-breaker only** (FT-REQ-21); never lowers or raises a score on its own. Decided 2026-10-27 |
| **View basis** — seen on which views / image set: MLO only, tomosynthesis only, low-energy vs recombined (CEM), ABUS, post-contrast | Trailing clause in the location statement | SYN-09: "seen only on the MLO view, upper breast" has no medial/lateral — compatible with the whole upper half until localized. SYN-09, SYN-17: CEM describes one lesion twice (low-energy, recombined). SYN-02: ABUS depth is from the skin, not a third | FT-REQ-09, 11; `view_basis` in FT-DM-01. Radiologist-dictated, never Viewer slice numbers (device output, DC-13) |
| **Clip evidence** — "clip placed", "biopsy clip identified in the specimen", "surrounding the stereotactic clip" | Procedure report (clip created, with type); later imaging; excision pathology | SYN-08: core at 11:30, excision at "12 o'clock" with no distance — the clip is the only thing that carries the excision to the lesion. SYN-20: calcifications → MRI non-mass enhancement by clip. These are the links that set the final conclusion | FT-REQ-19; FT-DM-06. Clips migrate: a location discrepancy at a clip is shown, never used to reject the link; clip type resolves multiple clips (SYN-11 has three) |
| **Specimen labels** — Part A, B, C with each part's stated location, procedure kind and diagnosis | Specimen list and Diagnosis section of a pathology report | SYN-10: one report, ADH on the calcifications and fibroadenoma on the mass; collapsed to one record, the findings are wrong and the excision can't be routed. SYN-15: breast core and axillary FNA in one report | FT-REQ-18; FT-DM-06. One record per part; mostly a parsing rule (literal), not a model extraction |

**Not added, by decision:** patient and exam identity on faxed reports. That is upstream — a report has to reach the right patient and case before any of this runs — and it is an MDE requirement with its own test (section 5), not a matching input.

## 3. What stays fixed

- **BI-RADS category and pathology diagnosis stay literal-only.** The model may find where they appear; the value is the exact text by fixed rule, or flagged for staff coding. Nothing diagnostic is extracted (MA-REQ-023, BC-011).
- **The MDE never links.** It delivers "the radiologist said this corresponds to the mammographic mass." MA decides which finding that is and shows why.
- **No link sets a finding's state.** Status words and correspondence are matching inputs; conclusion comes from R-005; tracking from the recommendation (MA-REQ-005).
- **Every value carries its source span.** Shown beside the extracted field (BC-008, BC-011).
- **Text and document images only.** Follow-through operates on radiologist-dictated text and, at the MDE, on images *of documents* (faxes, scans). It never reads pixel data from a mammogram, an ultrasound, an MRI or a CEM, never uses Viewer-generated location values, and never does patient-specific modeling (DC-13; candidate file §0). Image registration is the separate lesion tracker, a device-level product outside this work.

## 4. Stage 1 — MA side (now)

**Inputs.** `records_clean.json` (144 records, 20 cases) and `reports.md` to check them against. `answer_key.json` stays closed until a results table exists. The candidate file for the rules and field definitions.

**Build.** A matcher that, for each case, takes the records in exam order and for each new record proposes zero or one continuation among that case's earlier records, by a stated deterministic rule on the record fields. Each proposal carries: the rule that fired, the fields it read, a score, and a status — **automatic** (would be applied without a person), **candidate** (presented for confirmation), or **none**. Design values to propose from the data, not fix in advance: the depth/distance tolerance per modality pair (FT-REQ-07), and what, if anything, beyond "one tracked finding + matching laterality" is safe to apply automatically (FT-REQ-24 — recommendation is nothing, until this stage measures it).

**Run 1 — clean.** The records as delivered. Measures the logic alone.

**Run 2 — degraded (after run 1 is reported).** The same records with fields removed, locations made vaguer, OCR-style errors introduced, and header/body conflicts. Measures whether the rule fails safely — sends to a person rather than links wrongly. Not distributed until run 1 is in, so the rule is designed for the signal before the noise.

**Results table** (one row per proposal, plus one per answer-key link the matcher missed):

| case | new record | proposed continuation | correct continuation | status (automatic / candidate / none) | rule fired | fields read | score | outcome (correct / wrong link / missed / wrongly automatic) |

**Report.** From the table:
1. Precision and recall of automatic links; precision of candidate links; the three wrong kinds counted separately (wrong link, missed link, link that should have been candidate but was automatic — the last is the one BC-008 cares about).
2. Per rule: how often it fired and how often it was right.
3. Per input in section 2: whether it was ever load-bearing — that is, whether any correct proposal depended on it. **This is the evidence for stage 2's extraction scope.**
4. The tolerances per modality pair that the data supported.
5. Anything in the records or answer key that looks wrong. The set was built by hand; it has errors.

## 5. Stage 2 — MDE side (after stage 1 reports)

Written from stage 1's results, not before: the MDE candidate requirements and data model additions are the extraction targets stage 1 proved load-bearing, in the record shape stage 1 consumed. Two tests follow, both separate from the matching test:

**Extraction.** The same 20 cases from `reports.md`, extracted by the MDE path, compared record by record to `records_clean.json`. Measures extraction on its own, with the matcher held fixed. The literal/model split applies: BI-RADS and diagnosis by fixed rule, descriptors by the bounded model, each with its source span.

**Fax identity.** A separate set of about eight faxed pathology transmissions as degraded document images (resolution, skew, noise, a corrupted DOB digit), one per failure mode: clean baseline; DOB degraded; near-name patient who doesn't exist; missing page; pages out of order; two patients in one transmission; cover sheet with no identifiers on page 1; preliminary then final under one accession. Pass/fail is whether each reaches the right patient and case, or the queue — never a wrong patient. The degraded-DOB case must go to the queue; attaching it correctly by luck is still a failure. A small physically degraded subset (printed, marked up, scanned) will be added to check the synthetic degradation against the real thing.

## 6. What comes back to the documents, and how

| Finding from the exploration | Route |
|---|---|
| A candidate inside the fence works | Proposed into the Requirements Document / Data Model by the normal path |
| A candidate outside BC-008 (geometric conversion) or BC-011 (new extracted descriptors) works and is needed | Recorded as the evidence for a BC-008 / BC-011 restatement under determination trigger 20 / 22; nothing changes until that goes through |
| A candidate is not load-bearing | Dropped from the candidate file with the reason |
| An input in section 2 turns out to need the model to infer rather than extract | Stops there; flagged, not built |

## 7. Working practice

- Questions and findings about the test set go to Rachel; the candidate file and this plan are the record, not chat.
- Nothing from this folder is cited in a controlled document until it has a version and a results table behind it.
- Lance owns the MDE → MA interface contract; anything stage 2 needs the interface to carry goes through him.

*Change notes.* **v0.1 — 2026-10-27 (Rachel Orth):** first version, written with the v0.2 test set and candidate file v0.2.
