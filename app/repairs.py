from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable, Iterator
from types import MappingProxyType

from .case_data import (
    RepairCase,
    read_document_records,
    transform_document,
)
from .schemas import (
    BooleanSpec,
    Candidate,
    DriftReport,
    JoinSpec,
    PipelineConfig,
    RepairPlan,
    RepairProgram,
    RepairStep,
    SplitSpec,
)

PARAMETER_NAMES = {
    "field_sources",
    "delimiter",
    "format",
    "field",
    "strategy",
    "input_format",
    "true_values",
    "false_values",
    "path",
    "sources",
    "source",
    "split_fields",
    "separator",
}

ALLOWED_PARAMETERS = {
    "no_change": set(),
    "set_source_format": {"format"},
    "update_field_sources": {"field_sources"},
    "set_delimiter": {"delimiter"},
    "set_cast": {"field", "strategy"},
    "set_date_format": {"field", "input_format"},
    "set_boolean_values": {"field", "true_values", "false_values"},
    "set_record_path": {"path"},
    "set_join_source": {"field", "sources", "separator"},
    "set_split_source": {"source", "split_fields", "separator"},
    "escalate": set(),
}


def _provided_parameters(action: RepairPlan | RepairStep) -> set[str]:
    payload = action.model_dump()
    return {
        name
        for name in PARAMETER_NAMES
        if payload.get(name) is not None and payload.get(name) != []
    }


def _validate_parameters(action: RepairPlan | RepairStep) -> None:
    provided = _provided_parameters(action)
    allowed = ALLOWED_PARAMETERS[action.operation]
    unexpected = provided - allowed
    if unexpected:
        raise ValueError(f"Unexpected parameters: {', '.join(sorted(unexpected))}")
    missing = allowed - provided
    if missing:
        raise ValueError(f"Missing operation parameters: {', '.join(sorted(missing))}")


def apply_repair_step(config: PipelineConfig, step: RepairStep) -> PipelineConfig:
    _validate_parameters(step)
    patched = config.model_copy(deep=True)

    if step.operation == "set_source_format":
        patched.format = step.format
    elif step.operation == "update_field_sources":
        for update in step.field_sources:
            if update.output_field not in patched.fields:
                raise ValueError("field_sources may update existing output fields only")
            patched.fields[update.output_field] = update.source_field
    elif step.operation == "set_delimiter":
        patched.delimiter = step.delimiter
    elif step.operation == "set_cast":
        if step.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.casts[step.field] = step.strategy
    elif step.operation == "set_date_format":
        if step.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.date_formats[step.field] = step.input_format
    elif step.operation == "set_boolean_values":
        if step.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.booleans[step.field] = BooleanSpec(
            true_values=step.true_values,
            false_values=step.false_values,
        )
    elif step.operation == "set_record_path":
        patched.record_path = None if step.path == "$" else step.path
    elif step.operation == "set_join_source":
        if step.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.joins[step.field] = JoinSpec(
            sources=step.sources,
            separator=step.separator,
        )
    elif step.operation == "set_split_source":
        for split in step.split_fields:
            if split.output_field not in patched.fields:
                raise ValueError(f"Unknown output field: {split.output_field}")
            patched.splits[split.output_field] = SplitSpec(
                source=step.source,
                index=split.index,
                separator=step.separator,
            )
    return PipelineConfig.model_validate(patched.model_dump())


def program_from_legacy_plan(plan: RepairPlan) -> RepairProgram:
    if plan.operation == "no_change":
        decision = "unchanged"
        steps: list[RepairStep] = []
    elif plan.operation == "escalate":
        decision = "escalate"
        steps = []
    else:
        decision = "repair"
        step_payload = plan.model_dump(
            exclude={"confidence", "evidence", "rationale"},
            exclude_none=True,
        )
        steps = [RepairStep.model_validate(step_payload)]
    return RepairProgram(
        decision=decision,
        steps=steps,
        confidence=plan.confidence,
        evidence=plan.evidence,
        rationale=plan.rationale,
    )


def apply_repair_program(
    config: PipelineConfig, program: RepairProgram
) -> PipelineConfig:
    patched = config.model_copy(deep=True)
    for step in program.steps:
        patched = apply_repair_step(patched, step)
    return PipelineConfig.model_validate(patched.model_dump())


def apply_repair_plan(config: PipelineConfig, plan: RepairPlan) -> PipelineConfig:
    return apply_repair_program(config, program_from_legacy_plan(plan))


def _step_affected_output_fields(
    config: PipelineConfig, step: RepairStep
) -> frozenset[str]:
    if step.operation == "update_field_sources":
        return frozenset(item.output_field for item in step.field_sources)
    if step.operation in {"set_cast", "set_date_format", "set_boolean_values"}:
        return frozenset({step.field}) if step.field else frozenset()
    if step.operation == "set_join_source":
        return frozenset({step.field}) if step.field else frozenset()
    if step.operation == "set_split_source":
        return frozenset(item.output_field for item in step.split_fields)
    if step.operation in {"set_source_format", "set_delimiter", "set_record_path"}:
        return frozenset(config.fields)
    return frozenset()


def affected_output_fields(
    config: PipelineConfig,
    action: RepairPlan | RepairProgram,
) -> frozenset[str]:
    program = (
        action if isinstance(action, RepairProgram) else program_from_legacy_plan(action)
    )
    affected: set[str] = set()
    current = config
    for step in program.steps:
        affected.update(_step_affected_output_fields(current, step))
        current = apply_repair_step(current, step)
    return frozenset(affected)


def _canonical_step(step: RepairStep) -> str:
    return json.dumps(
        step.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        sort_keys=True,
        separators=(",", ":"),
    )


def _candidate(step: RepairStep, summary: str) -> Candidate:
    identifier = hashlib.sha256(_canonical_step(step).encode("utf-8")).hexdigest()[:12]
    return Candidate(id=f"c_{identifier}", step=step, summary=summary)


class CandidateCatalogue:
    def __init__(self, candidates: Iterable[Candidate]):
        items: dict[str, Candidate] = {}
        for candidate in candidates:
            existing = items.get(candidate.id)
            if existing is not None and existing.step != candidate.step:
                raise ValueError("candidate identifier collision")
            items[candidate.id] = candidate.model_copy(deep=True)
        self._items = MappingProxyType(items)

    def __iter__(self) -> Iterator[Candidate]:
        return iter(
            tuple(item.model_copy(deep=True) for item in self._items.values())
        )

    def __len__(self) -> int:
        return len(self._items)

    def step_for(self, identifier: str) -> RepairStep:
        try:
            return self._items[identifier].step.model_copy(deep=True)
        except KeyError as exc:
            raise ValueError(f"unknown candidate: {identifier}") from exc

    def select(self, identifiers: list[str]) -> list[RepairStep]:
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("candidate identifiers must be unique")
        return [self.step_for(identifier) for identifier in identifiers]


DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y")
JOIN_SEPARATORS = (" ", ",", "|", ";", ":")
MAX_CANDIDATES = 256


def _quoted_name(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _observed_config(case: RepairCase, report: DriftReport) -> PipelineConfig:
    return case.pipeline.model_copy(
        deep=True,
        update={
            "format": report.after.format,
            "delimiter": report.after.delimiter or case.pipeline.delimiter,
            "record_path": report.after.record_path,
        },
    )


def _value_matches_type(value: object, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "date":
        if not isinstance(value, str):
            return False
        try:
            from datetime import date

            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    return False


def _field_probe_passes(
    case: RepairCase,
    config: PipelineConfig,
    fields: Iterable[str],
) -> bool:
    targets = tuple(fields)
    try:
        before_records = transform_document(case.before, case.pipeline)
        probe_fields = {case.contract.unique_key, *targets}
        probe = config.model_copy(deep=True)
        probe.fields = {
            field: source
            for field, source in probe.fields.items()
            if field in probe_fields
        }
        for attribute in ("casts", "date_formats", "booleans", "joins", "splits"):
            values = getattr(probe, attribute)
            setattr(
                probe,
                attribute,
                {field: value for field, value in values.items() if field in probe_fields},
            )
        after_records = transform_document(
            case.after, PipelineConfig.model_validate(probe.model_dump())
        )
        if not after_records:
            return False
        if any(
            not _value_matches_type(record.get(field), case.contract.types[field])
            for record in after_records
            for field in targets
        ):
            return False
        if not (set(targets) & set(case.contract.preserve_values)):
            return True
        unique_key = case.contract.unique_key
        before_by_key = {str(record[unique_key]): record for record in before_records}
        after_by_key = {str(record[unique_key]): record for record in after_records}
        return set(before_by_key) == set(after_by_key) and all(
            before_by_key[key].get(field) == after_by_key[key].get(field)
            for key in before_by_key
            for field in targets
            if field in case.contract.preserve_values
        )
    except (KeyError, TypeError, ValueError):
        return False


def _boolean_step(
    case: RepairCase,
    report: DriftReport,
    observed: PipelineConfig,
    field: str,
) -> RepairStep | None:
    source = observed.fields[field]
    key_source = observed.fields.get(case.contract.unique_key)
    if not key_source:
        return None
    try:
        before = transform_document(case.before, case.pipeline)
        raw_after = read_document_records(case.after, observed)
        before_by_key = {
            str(record[case.contract.unique_key]): record[field] for record in before
        }
        observed_values: dict[str, bool] = {}
        for record in raw_after:
            key = str(record.get(key_source))
            raw_value = record.get(source)
            expected = before_by_key.get(key)
            if not isinstance(raw_value, (str, int, float, bool)) or not isinstance(
                expected, bool
            ):
                return None
            normalized = str(raw_value)
            previous = observed_values.get(normalized)
            if previous is not None and previous is not expected:
                return None
            observed_values[normalized] = expected
    except (KeyError, TypeError, ValueError):
        return None
    true_values = sorted(value for value, expected in observed_values.items() if expected)
    false_values = sorted(
        value for value, expected in observed_values.items() if not expected
    )
    if not true_values or not false_values:
        return None
    step = RepairStep(
        operation="set_boolean_values",
        field=field,
        true_values=true_values,
        false_values=false_values,
    )
    patched = apply_repair_step(observed, step)
    return step if _field_probe_passes(case, patched, [field]) else None


def build_candidate_catalogue(
    case: RepairCase, report: DriftReport
) -> CandidateCatalogue:
    if case.id != report.scenario_id:
        raise ValueError("case and report identifiers differ")
    candidates: list[Candidate] = []
    identifiers: set[str] = set()

    def add(step: RepairStep, summary: str) -> None:
        _validate_parameters(step)
        item = _candidate(step, summary)
        if item.id not in identifiers and len(candidates) < MAX_CANDIDATES:
            identifiers.add(item.id)
            candidates.append(item)

    config = case.pipeline
    observed = _observed_config(case, report)
    if report.after.format != config.format:
        add(
            RepairStep(operation="set_source_format", format=report.after.format),
            f"source format changed to {_quoted_name(report.after.format)}",
        )
    if report.after.format == "csv" and report.after.delimiter != config.delimiter:
        add(
            RepairStep(operation="set_delimiter", delimiter=report.after.delimiter),
            "CSV delimiter changed",
        )
    if report.after.format == "json" and report.after.record_path != config.record_path:
        path = report.after.record_path or "$"
        add(
            RepairStep(operation="set_record_path", path=path),
            f"record path changed to {_quoted_name(path)}",
        )

    explicit_updates: list[dict[str, str]] = []
    removed = set(report.removed_fields)
    added = set(report.added_fields)
    for output_field, old_source in sorted(config.fields.items()):
        if old_source not in removed:
            continue
        aliases = case.contract.source_aliases.get(output_field, [])
        explicit_sources = [source for source in aliases if source in added]
        sources = explicit_sources
        if not sources and output_field in case.contract.preserve_values and len(added) <= 16:
            sources = sorted(added)
        for source in sources:
            update = {"output_field": output_field, "source_field": source}
            add(
                RepairStep(operation="update_field_sources", field_sources=[update]),
                (
                    f"retarget {_quoted_name(output_field)} to observed field "
                    f"{_quoted_name(source)}"
                ),
            )
        if len(explicit_sources) == 1:
            explicit_updates.append(
                {"output_field": output_field, "source_field": explicit_sources[0]}
            )
    if len(explicit_updates) > 1 and len(
        {item["source_field"] for item in explicit_updates}
    ) == len(explicit_updates):
        add(
            RepairStep(
                operation="update_field_sources", field_sources=explicit_updates
            ),
            f"apply {len(explicit_updates)} documented source aliases",
        )

    for field, expected_type in sorted(case.contract.types.items()):
        if field not in observed.fields:
            continue
        if expected_type in {"string", "integer", "number"}:
            strategies = {
                "string": ("string",),
                "integer": ("integer", "integer_grouped", "integer_from_float"),
                "number": ("number",),
            }[expected_type]
            for strategy in strategies:
                if observed.casts.get(field) == strategy:
                    continue
                step = RepairStep(
                    operation="set_cast", field=field, strategy=strategy
                )
                if _field_probe_passes(
                    case, apply_repair_step(observed, step), [field]
                ):
                    add(
                        step,
                        f"parse {_quoted_name(field)} as {_quoted_name(strategy)}",
                    )
        elif expected_type == "date":
            for input_format in DATE_FORMATS:
                if observed.date_formats.get(field) == input_format:
                    continue
                step = RepairStep(
                    operation="set_date_format",
                    field=field,
                    input_format=input_format,
                )
                if _field_probe_passes(
                    case, apply_repair_step(observed, step), [field]
                ):
                    add(step, f"parse {_quoted_name(field)} with an allowlisted date format")
        elif expected_type == "boolean":
            step = _boolean_step(case, report, observed, field)
            if step is not None:
                add(step, f"map observed boolean labels for {_quoted_name(field)}")

    added_names = sorted(added)
    if len(added_names) <= 6:
        for output_field, old_source in sorted(config.fields.items()):
            if old_source not in removed:
                continue
            for size in range(2, min(4, len(added_names)) + 1):
                for sources in itertools.permutations(added_names, size):
                    for separator in JOIN_SEPARATORS:
                        step = RepairStep(
                            operation="set_join_source",
                            field=output_field,
                            sources=list(sources),
                            separator=separator,
                        )
                        patched = apply_repair_step(observed, step)
                        if _field_probe_passes(case, patched, [output_field]):
                            add(
                                step,
                                f"join observed fields for {_quoted_name(output_field)}",
                            )

    split_outputs = [
        output_field
        for output_field, source in sorted(config.fields.items())
        if source in removed
    ]
    if 2 <= len(split_outputs) <= 4:
        for source in added_names:
            for indexes in itertools.permutations(range(len(split_outputs))):
                for separator in JOIN_SEPARATORS:
                    step = RepairStep(
                        operation="set_split_source",
                        source=source,
                        split_fields=[
                            {"output_field": field, "index": index}
                            for field, index in zip(split_outputs, indexes, strict=True)
                        ],
                        separator=separator,
                    )
                    patched = apply_repair_step(observed, step)
                    if _field_probe_passes(case, patched, split_outputs):
                        add(step, f"split observed field {_quoted_name(source)}")

    return CandidateCatalogue(candidates)


def authorize_repair_plan(
    report: DriftReport,
    plan: RepairPlan,
) -> tuple[bool, str]:
    """Prove that one operation corresponds to the observed structural drift."""
    config = report.current_pipeline
    before_fields = {field.name for field in report.before.fields}
    after_fields = {field.name for field in report.after.fields}

    if plan.operation == "no_change":
        return True, "operation does not mutate the pipeline"

    if plan.operation == "escalate":
        if report.current_failure == "No contract failure observed":
            return False, "escalation requires a failing baseline"
        before_profiles = {field.name: field for field in report.before.fields}
        after_profiles = {field.name: field for field in report.after.fields}
        preserved = set(report.contract.preserve_values)
        bounded_signal = (
            report.current_pipeline.format == "csv"
            and report.before.delimiter != report.after.delimiter
        ) or (
            report.current_pipeline.format == "json"
            and report.before.record_path != report.after.record_path
        )
        for output_field, source_field in config.fields.items():
            if output_field not in preserved:
                continue
            if source_field in before_fields and source_field not in after_fields:
                bounded_signal = bounded_signal or bool(report.added_fields)
                continue
            if source_field not in before_profiles or source_field not in after_profiles:
                continue
            parser_exists = output_field in (
                config.casts.keys()
                | config.date_formats.keys()
                | config.booleans.keys()
            )
            values_changed = (
                before_profiles[source_field].example_values
                != after_profiles[source_field].example_values
            )
            bounded_signal = bounded_signal or (parser_exists and values_changed)
        return (not bounded_signal), (
            "no source-supported bounded repair signal remains"
            if not bounded_signal
            else "observations support a bounded repair that must be evaluated first"
        )

    if plan.operation == "update_field_sources":
        if not plan.field_sources:
            return False, "no source-field updates were proposed"
        outputs = {item.output_field for item in plan.field_sources}
        sources = {item.source_field for item in plan.field_sources}
        if len(outputs) != len(plan.field_sources) or len(sources) != len(plan.field_sources):
            return False, "source-field updates must be one-to-one"
        pairs = [
            (
                item.output_field,
                config.fields.get(item.output_field),
                item.source_field,
            )
            for item in plan.field_sources
        ]
        authorized = all(
            old_source in before_fields
            and old_source not in after_fields
            and new_source not in before_fields
            and new_source in after_fields
            and (
                output_field in report.contract.preserve_values
                or report.contract.source_aliases.get(output_field)
                == [old_source, new_source]
            )
            for output_field, old_source, new_source in pairs
        )
        return authorized, (
            "every retarget maps a removed source to one newly observed source"
            if authorized
            else "retargets do not match removed and newly observed source fields"
        )

    if plan.operation == "set_delimiter":
        authorized = (
            config.format == "csv"
            and report.before.delimiter == config.delimiter
            and report.after.delimiter == plan.delimiter
            and plan.delimiter != config.delimiter
        )
        return authorized, (
            f"delimiter changed from {config.delimiter!r} to {plan.delimiter!r}"
            if authorized
            else "proposed delimiter does not match the detected delimiter transition"
        )

    if plan.operation == "set_record_path":
        authorized = (
            config.format == "json"
            and report.before.record_path == config.record_path
            and report.after.record_path == plan.path
            and plan.path != config.record_path
        )
        return authorized, (
            f"record path changed from {config.record_path!r} to {plan.path!r}"
            if authorized
            else "proposed record path does not match the detected path transition"
        )

    if plan.operation == "set_join_source":
        old_source = config.fields.get(plan.field or "")
        authorized = (
            old_source in before_fields
            and old_source not in after_fields
            and len(plan.sources) >= 2
            and len(set(plan.sources)) == len(plan.sources)
            and all(source in after_fields for source in plan.sources)
        )
        return authorized, (
            "one removed source is reconstructed from multiple observed sources"
            if authorized
            else "join sources do not correspond to the observed split"
        )

    if plan.operation == "set_split_source":
        old_sources = {
            config.fields.get(item.output_field) for item in plan.split_fields
        }
        authorized = (
            len(plan.split_fields) >= 2
            and len({item.output_field for item in plan.split_fields})
            == len(plan.split_fields)
            and len({item.index for item in plan.split_fields}) == len(plan.split_fields)
            and all(source in before_fields and source not in after_fields for source in old_sources)
            and plan.source not in before_fields
            and plan.source in after_fields
        )
        return authorized, (
            "multiple removed outputs are recovered from one newly observed source"
            if authorized
            else "split source does not correspond to the observed field consolidation"
        )

    if not plan.field or plan.field not in config.fields:
        return False, "operation does not target an existing output field"
    source = config.fields[plan.field]
    if source not in before_fields or source not in after_fields:
        return False, "the targeted source field is not present on both sides"

    if plan.operation == "set_cast":
        expected_strategies = {
            "string": {"string"},
            "integer": {"integer", "integer_grouped", "integer_from_float"},
            "number": {"number"},
        }
        expected_type = report.contract.types.get(plan.field)
        authorized = (
            plan.strategy in expected_strategies.get(expected_type, set())
            and config.casts.get(plan.field) != plan.strategy
        )
        return authorized, (
            f"cast strategy preserves the {expected_type} output contract"
            if authorized
            else "cast strategy is redundant or incompatible with the output contract"
        )

    if plan.operation == "set_date_format":
        authorized = (
            report.contract.types.get(plan.field) == "date"
            and config.date_formats.get(plan.field) != plan.input_format
        )
        return authorized, (
            "date parser changes while preserving the date output contract"
            if authorized
            else "date parser is redundant or targets a non-date contract"
        )

    if plan.operation == "set_boolean_values":
        current = config.booleans.get(plan.field)
        proposed = (frozenset(plan.true_values), frozenset(plan.false_values))
        previous = (
            (frozenset(current.true_values), frozenset(current.false_values))
            if current
            else None
        )
        authorized = (
            report.contract.types.get(plan.field) == "boolean"
            and proposed != previous
            and not (
                {value.casefold() for value in plan.true_values}
                & {value.casefold() for value in plan.false_values}
            )
        )
        return authorized, (
            "boolean vocabulary changes without overlapping truth values"
            if authorized
            else "boolean vocabulary is redundant, overlapping or targets another type"
        )

    return False, "operation is not authorized"
