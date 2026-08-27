"""Documentation that is an acceptance criterion, guarded as one.

Most documentation cannot usefully be tested. These lines can, because they are
not prose — they are the specific things M4-03 was required to name, and the
failure mode is silent: a section gets reorganised, a bullet gets tightened, and
the checklist quietly stops naming the operation that causes outages. Nothing
breaks, so nothing tells you.

The repository already asserts against non-Python files in tests (test_settings
parses docker-compose.yml), so this follows an established habit rather than
introducing one.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"


# The anchor CONTRIBUTING.md's checklist heading generates, and which
# docs/migrations.md, docs/layout.md and README.md all link to.
CHECKLIST_ANCHOR = "CONTRIBUTING.md#migration-review-checklist"


def _checklist_section() -> str:
    """The text of the checklist section only, not the whole guide.

    Scoped deliberately: asserting a word appears *somewhere* in a 300-line
    document proves almost nothing, and would keep passing after the checklist
    itself was gutted.
    """
    text = CONTRIBUTING.read_text()
    start = text.index("## Migration review checklist")
    end = text.index("\n## ", start + 1)
    return text[start:end]



def test_contributing_has_the_checklist_section():
    assert "## Migration review checklist" in CONTRIBUTING.read_text()




# ---------------------------------------------------------------------------
# The four operations the issue requires by name
# ---------------------------------------------------------------------------
# Each entry: a label for the failure message, and the patterns that must all
# appear. Patterns are case-insensitive and deliberately loose about wording —
# they pin the SUBJECT, not the sentence, so the prose stays free to improve.
DANGEROUS_OPERATIONS = [
    pytest.param(
        [r"non-nullable", r"default"],
        id="non-nullable column without a default",
    ),
    pytest.param(
        [r"CONCURRENTLY|AddIndexConcurrently", r"index"],
        id="index without CONCURRENTLY",
    ),
    pytest.param(
        [r"renam\w*", r"drop\w*", r"single deploy|one deploy"],
        id="rename or drop in a single deploy",
    ),
    pytest.param(
        [r"data migration", r"whole table|every row|entire table"],
        id="data migration loading the whole table",
    ),
]


@pytest.mark.parametrize("patterns", DANGEROUS_OPERATIONS)
def test_checklist_names_each_dangerous_operation(patterns):
    """M4-03 requires these four by name, not a general 'review carefully'.

    Naming them is the whole point: a reviewer who has not personally caused an
    outage does not know to look for them, and a generic instruction to be
    careful gives them nothing to check against.
    """
    section = _checklist_section()
    for pattern in patterns:
        assert re.search(pattern, section, re.IGNORECASE), (
            f"the migration review checklist no longer names {pattern!r}"
        )




# ---------------------------------------------------------------------------
# The guide does not promise what it already delivers
# ---------------------------------------------------------------------------
def test_guide_grows_table_promises_nothing_already_written():
    """"This guide grows" lists sections a future milestone adds.

    A row left in place after its section lands sends the reader looking for
    something that is already above them, and quietly erodes trust in the rest
    of the table.
    """
    text = CONTRIBUTING.read_text()
    table = text[text.index("## This guide grows") :]

    headings = {
        line.lstrip("#").strip().lower()
        for line in text.splitlines()
        if line.startswith("## ")
    }

    for row in re.findall(r"^\| (.+?) \| ", table, re.MULTILINE):
        if row.lower() in {"section to come", "---"}:
            continue
        assert row.lower() not in headings, (
            f'"This guide grows" still promises "{row}", but that section already exists'
        )


def test_makefile_exposes_the_drift_check():
    """The checklist tells reviewers to run it, so it has to be discoverable."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert re.search(r"^migrations-check:.*##", makefile, re.MULTILINE), (
        "make migrations-check must exist and carry a ## comment so `make help` lists it"
    )
