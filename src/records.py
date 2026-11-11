"""Finding follow-through — record store.

Ponytail: stdlib only (json + tomllib). One class, load + save, nothing else.
The store loads every report/exam/finding in data/records_clean.json into per-case
lists and can serialize the same shape back out. Reports (reports.md) are attached
to each case as raw text so the matcher can show source spans beside its decisions.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator


class RecordError(Exception):
    """Raised when the JSON shape is not what the FT-DM says it should be."""


REQUIRED_RECORD_FIELDS = (
    "finding_id", "exam_id", "modality", "laterality", "site", "finding_type",
    "location",
)

# Record-shape fields: the fields a record MUST carry (the FT-DM record shape).
# The matching code imports this single list; the records store and the matcher
# both depend on it, so the shape is defined here exactly once (ponytail: one source).


def detect_project_root(start: Path | None = None) -> Path:
    """Find the folder that contains config/config.toml, working up from `start`."""
    p = (start or Path.cwd()).resolve()
    for d in (p, *p.parents):
        if (d / "config" / "config.toml").is_file():
            return d
    raise FileNotFoundError("config/config.toml not found; run from inside the project or pass a root")


def load_config(root: Path | None = None) -> dict[str, Any]:
    root = resolve_root(root)
    with open(root / "config" / "config.toml", "rb") as f:
        return tomllib.load(f)


def resolve_root(root: Path | str | None = None) -> Path:
    if root is not None:
        p = Path(root).expanduser().resolve()
        if not (p / "config" / "config.toml").is_file():
            raise FileNotFoundError(f"{p} does not contain config/config.toml")
        return p
    return detect_project_root()


def _resolve(root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p).resolve()


def _split_reports_md(text: str) -> dict[str, str]:
    """Split reports.md into {case_id: text}. Header shape: '## SYN-01 (age 58)'."""
    cases: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^##\s+(\S+)", line)
        if m and re.fullmatch(r"SYN-\d+", m.group(1)):
            if current:
                cases[current] = "".join(buf)
            current, buf = m.group(1), []
        elif line.strip() == "---" and buf and any(l.strip() for l in buf):
            # section separator between cases; ignore
            continue
        else:
            if current:
                buf.append(line)
    if current:
        cases[current] = "".join(buf)
    return cases


@dataclass
class Records:
    """All find records, loaded from data/records_clean.json."""

    root: Path
    metadata: dict[str, Any]
    cases: list[dict[str, Any]] = field(default_factory=list)
    reports: dict[str, str] = field(default_factory=dict)  # case_id -> raw text

    # -- access ---------------------------------------------------------------

    @property
    def by_case(self) -> dict[str, dict[str, Any]]:
        return {c["case_id"]: c for c in self.cases}

    def case(self, case_id: str) -> dict[str, Any]:
        by = self.by_case
        if case_id not in by:
            raise KeyError(f"unknown case {case_id!r}; have {sorted(by)}")
        return by[case_id]

    @staticmethod
    def _record_key(rec: dict[str, Any]) -> tuple[str, str]:
        return (rec["exam_id"], rec["finding_id"])

    def records_in_order(self, case: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Exam order, then finding order as given. Stable: exam_id is E1..En."""
        for exam in case.get("exams", []):
            for rec in exam.get("extracted_findings", []):
                yield rec

    def all_records(self) -> list[tuple[str, dict[str, Any]]]:
        """(case_id, record) pairs in stable test-set order."""
        out: list[tuple[str, dict[str, Any]]] = []
        for case in self.cases:
            for rec in self.records_in_order(case):
                out.append((case["case_id"], rec))
        return out

    # -- save -----------------------------------------------------------------

    def save(self, path: Path | str | None = None) -> Path:
        """Write the loaded dataset back out in the same shape. Default: the source file."""
        root = self.root
        path = Path(path) if path else _resolve(root, "data/records_clean.json")
        payload = {
            "metadata": self.metadata,
            "cases": [
                {
                    "case_id": c["case_id"],
                    **{k: c[k] for k in c if k != "case_id"},
                }
                for c in self.cases
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.write("\n")
        return path


def _validate_record(rec: dict[str, Any]) -> None:
    missing = [k for k in REQUIRED_RECORD_FIELDS if k not in rec]
    if missing:
        raise RecordError(f"record missing fields {missing}: {rec.get('finding_id')!r}")


def load(start: Path | str | None = None, root: Path | str | None = None) -> Records:
    """Load records_clean.json (+ reports.md if present).

    `start`: the records JSON file to read (default: auto-detected under the project root).
    `root`:  the project root (where config/config.toml lives). Used for relative paths.
    Both default to project detection, so `load()` Just Works from anywhere in the tree.
    """
    if root is None:
        root = detect_project_root()
    else:
        root = Path(root).expanduser().resolve()
    if (root / "config" / "config.toml").is_file():
        cfg = load_config(root)
    else:
        cfg = {"paths": {}}

    if start is not None:
        rec_path = Path(start).expanduser().resolve()
    else:
        rec_path = _resolve(root, cfg["paths"].get("records", "data/records_clean.json"))
    with open(rec_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for case in data.get("cases", []):
        for exam in case.get("exams", []):
            for rec in exam.get("extracted_findings", []):
                _validate_record(rec)

    reports_path = _resolve(root, cfg["paths"].get("reports", "data/reports.md"))
    reports_text = reports_path.read_text(encoding="utf-8") if reports_path.is_file() else ""

    return Records(
        root=root,
        metadata=data.get("metadata", {}),
        cases=data.get("cases", []),
        reports=_split_reports_md(reports_text),
    )


def main() -> None:
    """Self-check: loads the dataset, prints a one-line summary per case, round-trips to /tmp."""
    r = load()
    n_rec = sum(len(e.get("extracted_findings", [])) for c in r.cases for e in c.get("exams", []))
    print(f"loaded: {len(r.cases)} cases, {n_rec} records")
    for case in r.cases:
        recs = list(r.records_in_order(case))
        print(f"  {case['case_id']}  {len(recs)} records  "
              f"{[x['finding_id'].split('-', 2)[2] for x in recs]}")
    out = r.save("results/_records_roundtrip.json")
    with open(out) as f:
        n = len(json.load(f)["cases"])
    assert n == len(r.cases), "round-trip lost a case"
    print(f"round-trip ok -> {out}")


if __name__ == "__main__":
    main()
