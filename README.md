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