from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmark" / "external" / "manifest.json"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_SCANNED_ROWS = 250_000
MAX_OUTPUT_ROWS = 1_000
MAX_FIELDS = 256
MAX_CELL_CHARS = 100_000


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise ValueError("source exceeds the download-size limit")
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError("source exceeds the download-size limit")
    return payload


def _select(source: bytes, selector: dict[str, Any]) -> str:
    encoding = selector.get("encoding", "utf-8-sig")
    text = source.decode(encoding)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader)
    except StopIteration:
        raise ValueError("source has no CSV header") from None
    if not headers or any(not header for header in headers):
        raise ValueError("source has an empty CSV header")
    if len(headers) > MAX_FIELDS:
        raise ValueError("source exceeds the CSV field limit")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise ValueError(f"source has duplicate CSV headers: {', '.join(duplicates)}")
    fields = selector["fields"]
    if len(fields) != len(set(fields)):
        raise ValueError("selector fields must be unique")
    missing = set(fields) - set(headers)
    if missing:
        raise ValueError(f"source is missing fields: {sorted(missing)}")

    filters = selector.get("filters", {})
    if set(filters) - set(headers):
        raise ValueError("selector filters reference missing fields")
    limit = selector["limit"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_OUTPUT_ROWS:
        raise ValueError("selector limit is outside the allowed range")
    rows: list[dict[str, str]] = []
    for row_number, cells in enumerate(reader, start=2):
        if row_number - 1 > MAX_SCANNED_ROWS:
            raise ValueError("source exceeds the CSV row-scan limit")
        if len(cells) != len(headers):
            raise ValueError(
                f"source row {row_number} has {len(cells)} cells; expected {len(headers)}"
            )
        if any(len(cell) > MAX_CELL_CHARS for cell in cells):
            raise ValueError(f"source row {row_number} exceeds the cell-size limit")
        row = dict(zip(headers, cells, strict=True))
        if filters and not all(
            str(row[field]) == str(value) for field, value in filters.items()
        ):
            continue
        rows.append(row)
        if len(rows) == limit:
            break
    if len(rows) != limit:
        raise ValueError(
            f"selector expected {limit} rows and found {len(rows)}"
        )

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({field: row[field] for field in fields} for row in rows)
    return output.getvalue()


def materialize(*, verify_only: bool = False) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        for version in ("before", "after"):
            source_spec = case["sources"][version]
            source = _download(source_spec["url"])
            digest = hashlib.sha256(source).hexdigest()
            if digest != source_spec["sha256"]:
                raise ValueError(f"{case['id']} {version} source hash changed")
            rendered = _select(source, case["selectors"][version])
            destination = MANIFEST.parent / case[version]
            if verify_only:
                if not destination.is_file() or destination.read_text() != rendered:
                    raise ValueError(f"{case['id']} {version} fixture is not reproducible")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    materialize(verify_only=arguments.verify)
