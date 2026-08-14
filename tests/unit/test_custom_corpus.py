from __future__ import annotations

from collections import Counter

from app.benchmark import load_scenarios, scenario_case
from app.case_data import inspect_case
from app.repairs import build_candidate_catalogue
from app.synthesis import canonical_pipeline_hash, search_catalogue, verify_program


def _evaluated_cases():
    for scenario in load_scenarios(suite="custom"):
        case = scenario_case(scenario)
        catalogue = build_candidate_catalogue(case, inspect_case(case))
        program = search_catalogue(case, catalogue)
        yield scenario, program, verify_program(case, program, catalogue)


def test_custom_corpus_covers_formats_steps_depths_and_safe_terminals() -> None:
    evaluated = list(_evaluated_cases())
    transitions = {scenario.transition for scenario, _, _ in evaluated}
    operations = {
        step.operation for _, program, _ in evaluated for step in program.steps
    }
    repaired_depths = {
        len(program.steps)
        for _, program, result in evaluated
        if result.status == "repaired"
    }
    statuses = Counter(result.status for _, _, result in evaluated)

    assert transitions == {
        "csv_to_csv",
        "csv_to_json",
        "json_to_csv",
        "json_to_json",
    }
    assert operations == {
        "set_source_format",
        "update_field_sources",
        "set_delimiter",
        "set_cast",
        "set_date_format",
        "set_boolean_values",
        "set_record_path",
        "set_join_source",
        "set_split_source",
    }
    assert repaired_depths >= {1, 2, 3, 4, 5, 6}
    assert set(statuses) == {"unchanged", "repaired", "escalated"}
    assert statuses["escalated"] >= 6


def test_custom_corpus_matches_frozen_terminal_and_config_oracles() -> None:
    for scenario, _program, result in _evaluated_cases():
        assert result.status == scenario.expected_status
        config = result.patched_pipeline or scenario.pipeline
        assert canonical_pipeline_hash(config) == scenario.expected_pipeline_sha256


def test_custom_holdout_contains_every_terminal_without_expected_programs() -> None:
    holdout = [
        scenario
        for scenario in load_scenarios(suite="custom")
        if scenario.split == "holdout"
    ]

    assert {scenario.expected_status for scenario in holdout} == {
        "unchanged",
        "repaired",
        "escalated",
    }
    assert len(holdout) >= 8
    assert all(scenario.expected_plan is None for scenario in holdout)
