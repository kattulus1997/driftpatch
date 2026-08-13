from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set the bounded Cloud Storage source to its baseline or drift snapshot."
    )
    parser.add_argument("state", choices=("baseline", "drift"))
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    if not PROJECT_ID.fullmatch(args.project):
        parser.error("--project must be a valid dedicated Google Cloud project ID")

    suffix = "before" if args.state == "baseline" else "after"
    source = ROOT / "benchmark" / "fixtures" / f"column-rename-{suffix}.csv"
    destination = f"gs://{args.project}-live-source/column-rename.csv"
    subprocess.run(
        [
            "gcloud",
            "storage",
            "cp",
            str(source),
            destination,
            "--project",
            args.project,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
