from __future__ import annotations

from .schemas import BooleanSpec, JoinSpec, PipelineConfig, RepairPlan, SplitSpec


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

    if plan.operation == "escalate":
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
