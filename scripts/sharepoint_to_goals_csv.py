"""Turn the legacy SharePoint "Copleston PM" export into a goals.csv for import.

Standalone helper — not part of the Django app, not imported by it. Run it
locally against the SharePoint export; upload the result at /import/.

Why this exists
---------------
The SharePoint list holds TWO parallel blocks of goal review columns:

    Review of Goal 1 / 2 / 3                    the review of the PREVIOUS
    Review of Goal 1 / 2 / 3 Teacher Comments   year's goals, written in Sept

    Goal 1 / 2 / 3 Review                       the interim review of THAT
    Goal 1 / 2 / 3 Teacher Review               year's goals, from Dec onwards

The original bulk import took the first block and attached it to the goals of
the year the row belongs to — so reviews of the 2024/25 goals ended up on the
2025/26 Goal rows. This script emits the SECOND block, which is the one that
actually reviews the row's own goals and was never imported.

It writes ONLY the review columns. Titles, steps and success criteria are left
out of the file entirely, so the import cannot touch them (a column absent from
the CSV is never written — see data_import.services._set_if_present).

Usage
-----
    python scripts/sharepoint_to_goals_csv.py EXPORT.csv OUT.csv --academic-year 2025

    # the September reviews instead, if you ever need them:
    python scripts/sharepoint_to_goals_csv.py EXPORT.csv OUT.csv \
        --academic-year 2024 --block previous

OUT.csv holds named staff performance commentary. Keep it outside the
repository — the script refuses to write inside it.
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Goal N -> the goal_type the importer expects. Fixed mapping, matching
# data_import.services.GOAL_TYPE_ORDER (1/2/3 = Standards/Personal/Leadership).
GOAL_TYPES = {1: "STANDARDS", 2: "PERSONAL", 3: "LEADERSHIP"}

# The two blocks, keyed by goal number: (coach column, teacher column).
BLOCKS = {
    # Reviews of THIS row's own goals — interim, December onwards. The set the
    # import missed, and the one the Head is asking for.
    "current": {
        1: ("Goal 1 Review", "Goal 1 Teacher Review"),
        2: ("Goal 2 Review", "Goal 2 Teacher Review"),
        3: ("Goal 3 Review", "Goal 3 Teacher Review"),
    },
    # Reviews of the PREVIOUS year's goals, written at the start of the year.
    # The set that was imported, one year out of place.
    "previous": {
        1: ("Review of Goal 1", "Review of Goal 1 Teacher Comments"),
        2: ("Review of Goal 2", "Review of Goal 2 Teacher Comments"),
        3: ("Review of Goal 3", "Review of Goal 3 Teacher Comments"),
    },
}

EMAIL_COLUMN = "Email Address"

OUTPUT_COLUMNS = [
    "teacher_email",
    "academic_year",
    "goal_type",
    "teacher_review_comment",
    "coach_review_comment",
]

_BLOCK_BREAK = re.compile(
    r"</\s*(p|div|li|tr|h[1-6]|ul|ol|table|blockquote)\s*>|<\s*br\s*/?>",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 bytes that were decoded as cp1252 ("â" for an em dash).

    Only applied when the round trip is lossless, so text that was never
    mangled is returned untouched.
    """
    if "Ã" not in text and "â" not in text:
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def html_to_text(raw: str) -> str:
    """Flatten SharePoint's rich text into the plain text the app stores.

    The app's fields are plain TextFields rendered with `linebreaksbr` and
    escaped, so any markup left in would display as literal tags.
    """
    if not raw:
        return ""
    text = _BLOCK_BREAK.sub("\n", raw)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = repair_mojibake(text)
    # &#160; becomes a non-breaking space; normalise it so "blank" really is.
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def rows_from_export(export_path: Path, academic_year: int, block: str):
    columns = BLOCKS[block]
    with open(export_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [
            name
            for pair in columns.values()
            for name in pair
            if name not in (reader.fieldnames or [])
        ]
        if EMAIL_COLUMN not in (reader.fieldnames or []):
            missing.append(EMAIL_COLUMN)
        if missing:
            raise SystemExit(
                "Export is missing expected column(s): " + ", ".join(sorted(set(missing)))
            )

        for source in reader:
            email = (source.get(EMAIL_COLUMN) or "").strip().lower()
            if not email:
                continue
            for number, (coach_col, teacher_col) in columns.items():
                yield {
                    "teacher_email": email,
                    "academic_year": academic_year,
                    "goal_type": GOAL_TYPES[number],
                    "teacher_review_comment": html_to_text(source.get(teacher_col, "")),
                    "coach_review_comment": html_to_text(source.get(coach_col, "")),
                }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="the SharePoint export CSV")
    parser.add_argument("output", type=Path, help="goals.csv to write (outside the repo)")
    parser.add_argument(
        "--academic-year",
        type=int,
        required=True,
        help="start_year to stamp on every row (2025 for 2025/26)",
    )
    parser.add_argument(
        "--block",
        choices=sorted(BLOCKS),
        default="current",
        help=(
            "'current' (default) = reviews of the row's own goals, the interim "
            "set; 'previous' = the September reviews of last year's goals"
        ),
    )
    args = parser.parse_args(argv)

    if not args.export.exists():
        raise SystemExit(f"Export not found: {args.export}")

    out = args.output.expanduser().resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        raise SystemExit(
            f"Refusing to write inside the repository ({REPO_ROOT}).\n"
            "The output holds named staff performance commentary and a commit "
            "of this repo deploys to production. Choose a path outside it."
        )

    rows = list(rows_from_export(args.export, args.academic_year, args.block))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    with_text = sum(
        1 for r in rows if r["coach_review_comment"] or r["teacher_review_comment"]
    )
    teachers = len({r["teacher_email"] for r in rows})
    print(f"Wrote {len(rows)} row(s) for {teachers} teacher(s) to {out}")
    print(f"  {with_text} row(s) carry review text; {len(rows) - with_text} are blank.")
    print(
        "\nBlank rows are intentional — tick 'Blank review cells clear the stored\n"
        "comment' on the goals upload so they erase the misplaced text."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
