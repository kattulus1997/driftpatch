from app.benchmark import load_scenario
from app.gate import search_scenario, verify_scenario_program
from app.repairs import program_from_legacy_plan


def test_scenario_wrapper_returns_the_same_usable_verified_repair() -> None:
    scenario = load_scenario("column-rename")
    program = program_from_legacy_plan(scenario.expected_plan)

    result = verify_scenario_program(scenario.id, program)

    assert result.status == "repaired"
    assert result.patched_pipeline is not None
    assert result.patched_pipeline.fields["name"] == "full_name"
    assert result.patched_pipeline_hash is not None


def test_scenario_search_keeps_a_compatible_addition_unchanged() -> None:
    result = search_scenario("compatible-addition")

    assert result.status == "unchanged"
    assert result.program is not None
    assert result.program.decision == "unchanged"
