from __future__ import annotations

from .schemas import (
    BooleanSpec,
    DriftReport,
    JoinSpec,
    PipelineConfig,
    RepairPlan,
    SplitSpec,
)

PARAMETER_NAMES = {
    "field_sources",
    "delimiter",
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


def _provided_parameters(plan: RepairPlan) -> set[str]:
    payload = plan.model_dump()
    return {
        name
        for name in PARAMETER_NAMES
        if payload[name] is not None and payload[name] != []
    }


def _validate_parameters(plan: RepairPlan) -> None:
    provided = _provided_parameters(plan)
    allowed = ALLOWED_PARAMETERS[plan.operation]
    unexpected = provided - allowed
    if unexpected:
        raise ValueError(f"Unexpected parameters: {', '.join(sorted(unexpected))}")
    missing = allowed - provided
    if missing:
        raise ValueError(f"Missing operation parameters: {', '.join(sorted(missing))}")


def apply_repair_plan(config: PipelineConfig, plan: RepairPlan) -> PipelineConfig:
    _validate_parameters(plan)
    patched = config.model_copy(deep=True)

    if plan.operation in {"no_change", "escalate"}:
        return patched
    if plan.operation == "update_field_sources":
        for update in plan.field_sources:
            if update.output_field not in patched.fields:
                raise ValueError("field_sources may update existing output fields only")
            patched.fields[update.output_field] = update.source_field
    elif plan.operation == "set_delimiter":
        patched.delimiter = plan.delimiter
    elif plan.operation == "set_cast":
        if plan.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.casts[plan.field] = plan.strategy
    elif plan.operation == "set_date_format":
        if plan.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.date_formats[plan.field] = plan.input_format
    elif plan.operation == "set_boolean_values":
        if plan.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.booleans[plan.field] = BooleanSpec(
            true_values=plan.true_values,
            false_values=plan.false_values,
        )
    elif plan.operation == "set_record_path":
        patched.record_path = plan.path
    elif plan.operation == "set_join_source":
        if plan.field not in patched.fields:
            raise ValueError("Unknown output field")
        patched.joins[plan.field] = JoinSpec(
            sources=plan.sources,
            separator=plan.separator,
        )
    elif plan.operation == "set_split_source":
        for split in plan.split_fields:
            if split.output_field not in patched.fields:
                raise ValueError(f"Unknown output field: {split.output_field}")
            patched.splits[split.output_field] = SplitSpec(
                source=plan.source,
                index=split.index,
                separator=plan.separator,
            )
    return PipelineConfig.model_validate(patched.model_dump())


def affected_output_fields(
    config: PipelineConfig,
    plan: RepairPlan,
) -> frozenset[str]:
    if plan.operation == "update_field_sources":
        return frozenset(item.output_field for item in plan.field_sources)
    if plan.operation in {"set_cast", "set_date_format", "set_boolean_values"}:
        return frozenset({plan.field}) if plan.field else frozenset()
    if plan.operation == "set_join_source":
        return frozenset({plan.field}) if plan.field else frozenset()
    if plan.operation == "set_split_source":
        return frozenset(item.output_field for item in plan.split_fields)
    if plan.operation in {"set_delimiter", "set_record_path"}:
        return frozenset(config.fields)
    return frozenset()


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
