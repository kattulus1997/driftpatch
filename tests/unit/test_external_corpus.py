from __future__ import annotations

import json
import re
import runpy
from collections import Counter
from pathlib import Path

from scripts.build_external_eval import OUTPUT, build
from scripts.fetch_external_corpus import MANIFEST, _select, materialize

ROOT = Path(__file__).resolve().parents[2]
COMMIT_URL = re.compile(r"raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/")


def test_external_corpus_is_reproducible_from_immutable_sources() -> None:
    materialize(verify_only=True)


def test_external_corpus_has_provenance_licenses_and_a_frozen_holdout() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert payload["frozen_at"] == "2026-08-12T16:23:00Z"
    assert len(cases) >= 6
    assert len({case["publisher"] for case in cases}) >= 4
    assert {case["split"] for case in cases} == {"calibration", "holdout"}
    assert len([case for case in cases if case["split"] == "holdout"]) >= 4
    assert all(case["transition"] == "historical_observed" for case in cases)
    assert all(case["license"] and case["license_url"] for case in cases)
    assert all(
        COMMIT_URL.search(source["url"])
        for case in cases
        for source in case["sources"].values()
    )
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        for case in cases
        for source in case["sources"].values()
    )


def test_holdout_covers_all_terminal_decisions_and_multiple_publishers() -> None:
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    holdout = [case for case in cases if case["split"] == "holdout"]
    statuses = Counter(case["expected_status"] for case in holdout)

    assert set(statuses) == {"unchanged", "repaired", "escalated"}
    assert statuses["escalated"] >= 2
    assert len({case["publisher"] for case in holdout}) >= 3
    assert len({case["id"] for case in cases}) == len(cases)
    assert all((ROOT / "benchmark" / "external" / case["before"]).is_file() for case in cases)
    assert all((ROOT / "benchmark" / "external" / case["after"]).is_file() for case in cases)


def test_external_eval_dataset_is_derived_from_the_frozen_holdout() -> None:
    current = OUTPUT.read_text(encoding="utf-8")
    build()
    assert OUTPUT.read_text(encoding="utf-8") == current
    payload = json.loads(current)
    case_ids = {case["eval_case_id"] for case in payload["eval_cases"]}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    expected = {case["id"] for case in manifest if case["split"] == "holdout"}

    assert case_ids == expected


def test_decision_metric_covers_every_demo_and_external_case() -> None:
    expected = runpy.run_path(ROOT / "tests" / "eval" / "decision_accuracy.py")[
        "EXPECTED"
    ]
    demo = json.loads((ROOT / "benchmark" / "scenarios.json").read_text())["scenarios"]
    external = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]

    custom = json.loads(
        (ROOT / "benchmark" / "custom" / "manifest.json").read_text()
    )["cases"]

    assert set(expected) == {case["id"] for case in [*demo, *external, *custom]}


def test_corpus_selector_rejects_duplicate_headers_and_ragged_rows() -> None:
    selector = {"fields": ["id", "name"], "limit": 1}

    for content, message in (
        (b"id,id\n1,2\n", "duplicate CSV headers"),
        (b"id,name\n1\n", "has 1 cells; expected 2"),
    ):
        try:
            _select(content, selector)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("ambiguous CSV must fail closed")
