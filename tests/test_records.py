"""ponytail self-check for src/records.py.

No framework. Run from anywhere:  python3 tests/test_records.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from records import _split_reports_md, load


def test_split_reports_md() -> None:
    text = (
        "# title\n\n"
        "intro line\n\n---\n\n"
        "## SYN-01 (age 58)\n\n### E1 · 2025-02-03 · DBT · screening\nreport text one\n"
        "---\n## SYN-02 (age 52)\n\n### E1 · 2025-03-10 · DBT · screening\nreport text two\n"
        "---\n## SYN-03\nbody three\n"
    )
    c = _split_reports_md(text)
    assert set(c) == {"SYN-01", "SYN-02", "SYN-03"}, f"got {set(c)}"
    assert "report text one" in c["SYN-01"]
    assert "report text two" in c["SYN-02"]
    assert "body three" in c["SYN-03"]
    print("ok: _split_reports_md")


def test_load_all_records() -> None:
    r = load()
    assert len(r.cases) == 20, f"expected 20 cases, got {len(r.cases)}"
    n = sum(len(e.get("extracted_findings", [])) for cse in r.cases for e in cse.get("exams", []))
    assert n == 144, f"expected 144 records, got {n}"
    # every record has the required FT-DM fields
    for cse in r.cases:
        for e in cse.get("exams", []):
            for rec in e.get("extracted_findings", []):
                for k in ("finding_id", "exam_id", "modality", "laterality", "site",
                          "finding_type", "location"):
                    assert k in rec, f"missing {k} in {rec.get('finding_id')}"
    # reports.md is split into the same 20 cases
    assert set(r.reports) == {c["case_id"] for c in r.cases}, \
        f"reports keys {set(r.reports)} != case ids"
    print("ok: load (20 cases, 144 records, reports split)")


def test_case_access() -> None:
    r = load()
    c1 = r.case("SYN-01")
    assert c1["case_id"] == "SYN-01"
    recs = list(r.records_in_order(c1))
    assert recs[0]["finding_id"] == "SYN-01-E1-F1"
    try:
        r.case("NOPE")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    print("ok: case access")


def test_save_roundtrip(tmp_path: Path) -> None:
    r = load()
    out = tmp_path / "_roundtrip.json"
    p = r.save(out)
    assert p == out
    # load back from an explicit file path (start=) against the same project root
    r2 = load(start=out, root=r.root)
    assert len(r2.cases) == len(r.cases)
    a = {(c["case_id"], e["exam_id"], f["finding_id"])
         for c in r.cases for e in c.get("exams", []) for f in e.get("extracted_findings", [])}
    b = {(c["case_id"], e["exam_id"], f["finding_id"])
         for c in r2.cases for e in c.get("exams", []) for f in e.get("extracted_findings", [])}
    assert a == b, "round-trip changed the record id set"
    # the round-tripped metadata is preserved
    assert r2.metadata.get("conventions"), "conventions metadata lost in round-trip"
    print("ok: save round-trip (same 144 findings)")


def main() -> None:
    """Standalone run (no pytest). Builds a throwaway temp dir, feeds it to each test."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_save_roundtrip(Path(td))
    test_split_reports_md()
    test_load_all_records()
    test_case_access()
    print("\nall records.py checks pass")


if __name__ == "__main__":
    main()
