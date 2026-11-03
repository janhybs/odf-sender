"""
Builds versions.json from an ODF messages CSV export.

For every file it keeps the highest version seen among the rows with
Validity "Ok" (Duplicate and other rows are ignored).

File names come from one of two variants:

  variant-A  {Competition}_{Code}_{Type}.xml
             SYOG2026_ARCXTEAM2-------------------------_DT_ENTRIES.xml

  variant-B  {Code}_{SubType}_{SubCode}_{Type}.xml
             BDM-------------------------------_ACR_145137_DT_PIC.xml

By default the variant is chosen per row: B when the row has a SubCode,
otherwise A. Use --variant to force one.

By default the result is merged into the existing versions.json (the
higher version wins, other entries stay). Use --replace to start from
an empty file instead. The old file is saved as versions.json.bak.

Usage:
  python csv_to_versions.py Odf_Messages.csv
  python csv_to_versions.py a.csv b.csv --dry-run
  python csv_to_versions.py Odf_Messages.csv --variant B --replace
"""

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "versions.json"

VARIANTS = {
    "A": "{Competition}_{Code}_{Type}.xml",
    "B": "{Code}_{SubType}_{SubCode}_{Type}.xml",
}

REQUIRED_COLUMNS = (
    "Competition",
    "Type",
    "SubType",
    "Code",
    "SubCode",
    "Ver",
    "Validity",
)


def file_name(row: dict[str, str], variant: str) -> str:
    if variant == "auto":
        variant = "B" if row["SubCode"].strip() else "A"

    return VARIANTS[variant].format(
        **{key: value.strip() for key, value in row.items() if key}
    )


def read_versions(
    csv_path: Path,
    variant: str,
    latest: dict[str, int],
) -> tuple[int, int]:
    """
    Raises latest[file name] to the highest Ver found in csv_path.
    Returns (rows read, rows skipped because Validity is not "Ok").
    """

    # utf-8-sig drops the BOM that the export starts with.
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        missing = [
            column
            for column in REQUIRED_COLUMNS
            if column not in (reader.fieldnames or [])
        ]

        if missing:
            raise ValueError(
                f"{csv_path.name}: missing column(s): "
                + ", ".join(missing)
            )

        rows = skipped = 0

        for line, row in enumerate(reader, start=2):
            rows += 1

            if row["Validity"].strip() != "Ok":
                skipped += 1
                continue

            try:
                version = int(row["Ver"])
            except ValueError:
                raise ValueError(
                    f"{csv_path.name} line {line}: "
                    f"invalid Ver {row['Ver']!r}"
                ) from None

            name = file_name(row, variant)

            if version > latest.get(name, -1):
                latest[name] = version

    return rows, skipped


def load_existing(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    try:
        return {str(name): int(version) for name, version in data.items()}
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            f"{path.name} is not in the {{file: version}} format"
        ) from None


def save(path: Path, versions: dict[str, int]) -> None:
    temp_file = path.with_suffix(".json.tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(versions, f, indent=2, sort_keys=True)
        f.write("\n")

    temp_file.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build versions.json from ODF messages CSV export(s).",
    )
    parser.add_argument(
        "csv_files",
        nargs="+",
        type=Path,
        help="CSV export(s) to read",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="versions file to write (default: next to this script)",
    )
    parser.add_argument(
        "--variant",
        choices=("auto", "A", "B"),
        default="auto",
        help="file name variant (default: auto, B if the row has a "
        "SubCode, else A)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="ignore the existing file instead of merging into it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change, write nothing",
    )
    args = parser.parse_args()

    latest: dict[str, int] = {}
    rows = skipped = 0

    try:
        for csv_path in args.csv_files:
            file_rows, file_skipped = read_versions(
                csv_path,
                args.variant,
                latest,
            )
            rows += file_rows
            skipped += file_skipped

        existing = {} if args.replace else load_existing(args.output)

    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    merged = dict(existing)
    added = raised = unchanged = kept_higher = 0

    for name, version in latest.items():
        old = existing.get(name)

        if old is None:
            added += 1
            merged[name] = version
        elif version > old:
            raised += 1
            merged[name] = version
        elif version == old:
            unchanged += 1
        else:
            kept_higher += 1

    print(
        f"{rows} row(s) read, {skipped} skipped (Validity not Ok), "
        f"{len(latest)} file(s) found."
    )
    print(
        f"{added} added, {raised} raised, {unchanged} unchanged, "
        f"{kept_higher} kept at a higher existing version."
    )
    if args.replace:
        print("Existing entries ignored (--replace).")
    else:
        print(
            f"{len(set(existing) - set(latest))} other existing "
            "entr(ies) left as they are."
        )

    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0

    if merged == existing:
        print("No changes, nothing was written.")
        return 0

    if args.output.exists():
        backup = args.output.with_suffix(".json.bak")
        shutil.copyfile(args.output, backup)
        print(f"Saved the old file as {backup.name}.")

    save(args.output, merged)
    print(f"Wrote {len(merged)} entries to {args.output}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
