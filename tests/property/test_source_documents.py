from __future__ import annotations

import csv
import io
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from app.case_data import profile_document, read_document_records
from app.schemas import PipelineConfig, SourceDocument

SAFE_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=" -_",
    ),
    min_size=1,
    max_size=24,
)
IDENTIFIERS = st.lists(
    st.integers(min_value=0, max_value=1_000_000),
    min_size=1,
    max_size=20,
    unique=True,
)


@settings(max_examples=40)
@given(ids=IDENTIFIERS, labels=st.lists(SAFE_TEXT, min_size=1, max_size=20))
def test_json_profile_is_invariant_to_record_order(
    ids: list[int], labels: list[str]
) -> None:
    rows = [
        {"id": identifier, "label": labels[index % len(labels)]}
        for index, identifier in enumerate(ids)
    ]
    forward = SourceDocument(format="json", content=json.dumps({"rows": rows}))
    reversed_rows = SourceDocument(
        format="json", content=json.dumps({"rows": list(reversed(rows))})
    )

    assert profile_document(forward) == profile_document(reversed_rows)


@settings(max_examples=40)
@given(
    delimiter=st.sampled_from([",", ";", "|", "\t"]),
    ids=IDENTIFIERS,
    labels=st.lists(SAFE_TEXT, min_size=1, max_size=20),
)
def test_csv_reader_round_trips_every_supported_delimiter(
    delimiter: str, ids: list[int], labels: list[str]
) -> None:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(["id", "label"])
    expected = []
    for index, identifier in enumerate(ids):
        label = labels[index % len(labels)]
        writer.writerow([identifier, label])
        expected.append({"id": str(identifier), "label": label})
    document = SourceDocument(format="csv", content=stream.getvalue())
    config = PipelineConfig(
        format="csv",
        delimiter=delimiter,
        fields={"id": "id", "label": "label"},
    )

    assert read_document_records(document, config) == expected
