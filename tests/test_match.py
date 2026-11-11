"""ponytail self-check for src/match.py's offline logic (no model calls).

Validates the response parser and the status/link invariants:
  - no link  =>  status must be 'none'
  - link must be an EARLIER record of the same case
  - a bad/hallucinated link_id is clamped or dropped, never invented
  - status 'automatic' with a valid earlier link is kept

Run:  python3 tests/test_match.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import match as M  # noqa: E402


CASE = {
    "case_id": "SYN-99",
    "exams": [
        {"exam_id": "E1", "extracted_findings": [
            {"finding_id": "SYN-99-E1-F1", "laterality": "R", "site": "breast",
             "finding_type": "mass", "location": {"clock": "10:00"}},
            {"finding_id": "SYN-99-E1-F2", "laterality": "L", "site": "breast",
             "finding_type": "calcifications", "location": {"quadrant": "UIQ"}},
        ]},
        {"exam_id": "E2", "extracted_findings": [
            {"finding_id": "SYN-99-E2-F1", "laterality": "R", "site": "breast",
             "finding_type": "mass", "location": {"clock": "10:00"}},
        ]},
        {"exam_id": "E3", "extracted_findings": [
            {"finding_id": "SYN-99-E3-F1", "laterality": "R", "site": "breast",
             "finding_type": "specimen", "location": {}},
        ]},
    ],
}
RECS = M._records(CASE)


def _rows(payload: list[dict]) -> list[dict]:
    return M.parse_response(CASE, json.dumps(payload), RECS)


def test_first_record_is_none() -> None:
    # E1-F1 is first in the case -> no earlier record -> even 'automatic' is coerced to 'none'
    rows = _rows([{"new_record": "SYN-99-E1-F1", "status": "automatic",
                   "link_id": None, "score": 100, "reason": "first"}])
    assert rows[0]["status"] == "none" and rows[0]["link"] is None, rows[0]
    print("ok: first record -> status 'none', no link")


def test_valid_link_kept() -> None:
    rows = _rows([{"new_record": "SYN-99-E1-F1", "status": "none", "link_id": None},
                  {"new_record": "SYN-99-E2-F1", "status": "automatic",
                   "link_id": "SYN-99-E1-F1", "score": 90}])
    by = {r["new_record"]: r for r in rows}
    assert by["SYN-99-E2-F1"]["status"] == "automatic"
    assert by["SYN-99-E2-F1"]["link"] == "SYN-99-E1-F1"
    print("ok: valid earlier link kept")


def test_hallucinated_link_dropped() -> None:
    rows = _rows([
        {"new_record": "SYN-99-E1-F1", "status": "none", "link_id": None},
        {"new_record": "SYN-99-E2-F1", "status": "automatic",
         "link_id": "SYN-99-E9-F9", "score": 80},  # E9 does not exist
    ])
    by = {r["new_record"]: r for r in rows}
    assert by["SYN-99-E2-F1"]["link"] is None
    # invariant: no link forces status 'none'
    assert by["SYN-99-E2-F1"]["status"] == "none"
    print("ok: hallucinated link_id dropped; status coerced to 'none'")


def test_later_record_can_not_be_link() -> None:
    # E3-F1 (E3) cannot link to E2-F1 (E2 is earlier, that is allowed) but
    # E1-F1 cannot link to E2-F1 (later) — assert the parser refuses a LATER link.
    rows = _rows([
        {"new_record": "SYN-99-E1-F1", "status": "automatic",
         "link_id": "SYN-99-E2-F1", "score": 70},  # E2 is LATER than E1 -> invalid
    ])
    assert rows[0]["link"] is None, rows[0]
    print("ok: a LATER record is not accepted as a link")


def test_malformed_json_fails_safely() -> None:
    rows = M.parse_response(CASE, "not json at all {", RECS)
    assert len(rows) == len(RECS)
    assert all(r["model_error"] for r in rows)
    assert all(r["status"] == "none" and r["link"] is None for r in rows)
    print("ok: malformed model output -> one safe 'none' row per record")


def test_header_conflict_triggers_review() -> None:
    case = {
        "case_id": "SYN-XX",
        "exams": [
            {"exam_id": "E1", "exam_laterality": "right",
             "header_laterality": "right", "extracted_findings": [
                 {"finding_id": "SYN-XX-E1-F1", "laterality": "R", "site": "breast",
                  "finding_type": "mass", "location": {"clock": "10:00"}}]},
            {"exam_id": "E2", "exam_laterality": "right",
             "header_laterality": "LEFT (conflicts with body)", "extracted_findings": [
                 {"finding_id": "SYN-XX-E2-F1", "laterality": "R", "site": "breast",
                  "finding_type": "mass", "location": {"clock": "10:00"}}]},
        ],
    }
    recs = M._records(case)
    # model says automatic for E2-F1, but the header conflict must force review
    rows = M.parse_response(case, json.dumps([
        {"new_record": "SYN-XX-E1-F1", "status": "none", "link_id": None},
        {"new_record": "SYN-XX-E2-F1", "status": "automatic", "link_id": "SYN-XX-E1-F1", "score": 100},
    ]), recs)
    e2 = {r["new_record"]: r for r in rows}["SYN-XX-E2-F1"]
    assert e2["review_required"] is True, e2
    print("ok: header/body laterality conflict -> review_required=True (even if model says automatic)")


def main() -> None:
    test_first_record_is_none()
    test_valid_link_kept()
    test_hallucinated_link_dropped()
    test_later_record_can_not_be_link()
    test_malformed_json_fails_safely()
    test_header_conflict_triggers_review()
    print("\nall match.py offline checks pass")


if __name__ == "__main__":
    main()
