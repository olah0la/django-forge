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


# ---------------------------------------------------------------------------
# M4-06: the dump commands must not read as a backup policy
# ---------------------------------------------------------------------------
# The same class of silently-failing criterion as the checklist above. A
# documented dump command is easily mistaken for a backup strategy, and the
# disclaimer is the first thing a later tightening pass would cut as
# "defensive" — leaving a template that appears to ship backups.
BACKUPS_DOC = REPO_ROOT / "docs" / "backups.md"


def test_backups_doc_disclaims_a_backup_strategy_before_teaching_the_commands():
    """The caveat has to come first, not in a footnote.

    Position is the assertion. A reader who has already copied the command has
    stopped reading, so a disclaimer below the examples is one nobody reaches.
    """
    text = BACKUPS_DOC.read_text()
    disclaimer = re.search(r"not a (production )?backup strategy", text, re.IGNORECASE)
    assert disclaimer, "docs/backups.md no longer says this is not a backup strategy"
    assert disclaimer.start() < text.index("make db-dump"), (
        "the disclaimer must appear before the first dump command, not after it"
    )


# What a real strategy has that a dump on a laptop does not. Naming them is the
# point: "this is not a backup strategy" tells a reader they are missing
# something without telling them what, which is not actionable.
REAL_STRATEGY_ELEMENTS = [
    pytest.param([r"point-in-time|PITR|WAL"], id="point-in-time recovery"),
    pytest.param([r"off-host|off-site|offsite"], id="storage off the host"),
    pytest.param([r"retention|rotation"], id="retention and rotation"),
    pytest.param([r"monitor\w*|alert"], id="monitoring that the backup ran"),
    pytest.param([r"rehears\w*|tested|untested|restore drill"], id="rehearsed restores"),
]


@pytest.mark.parametrize("patterns", REAL_STRATEGY_ELEMENTS)
def test_backups_doc_names_what_a_real_strategy_needs(patterns):
    text = BACKUPS_DOC.read_text()
    for pattern in patterns:
        assert re.search(pattern, text, re.IGNORECASE), (
            f"docs/backups.md no longer names {pattern!r} among what a real strategy needs"
        )


def test_dump_output_is_git_ignored():
    """M4-06 criterion 3. A dump holds real data; this is a template others clone.

    Anchored on purpose, so an application directory named `backups/` stays
    trackable — the same reasoning as /media/ above it.
    """
    assert "/backups/" in (REPO_ROOT / ".gitignore").read_text(), (
        "dump output must stay git-ignored"
    )


def test_makefile_exposes_dump_and_restore():
    makefile = (REPO_ROOT / "Makefile").read_text()
    for target in ("db-dump", "db-restore"):
        assert re.search(rf"^{target}:.*##", makefile, re.MULTILINE), (
            f"make {target} must exist and carry a ## comment so `make help` lists it"
        )


def test_restore_validates_the_dump_before_dropping_anything():
    """Order is the whole safety property, and it is invisible when wrong.

    The first version of db-restore dropped the database and only then found
    the file unreadable — a recovery tool that destroys data when handed the
    wrong path. Reversing these two lines restores that behaviour silently.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    recipe = makefile[makefile.index("\ndb-restore:") :]
    recipe = recipe[: recipe.index("\n##@")]

    # Command lines only. Matching `pg_restore -l` anywhere would find the
    # comment that explains this rule, which sits above both commands and
    # would keep the test green after the check itself moved.
    commands = "\n".join(
        line for line in recipe.splitlines() if not line.lstrip().startswith("@#")
    )
    preflight = commands.index("pg_restore -l")
    drop = commands.index("DROP DATABASE")
    assert preflight < drop, (
        "db-restore must list the archive before dropping the database, "
        "or a wrong FILE= destroys the database and only then reports the error"
    )


# ---------------------------------------------------------------------------
# M6-05: the shutdown sequence has to stay written down, and stay ordered
# ---------------------------------------------------------------------------
# The same class of silently-failing criterion as the two above. The sequence is
# only useful as an ORDER — "readiness fails, then the socket closes, then
# requests finish, then connections close" — and a tightening pass that turns
# the table into prose can drop a step without anything failing. The steps are
# also the checklist someone reads at 3am while a deploy is dropping requests.
OPS_DOC = REPO_ROOT / "docs" / "ops.md"

SHUTDOWN_SEQUENCE = [
    pytest.param([r"SIGTERM"], id="the signal that starts it"),
    pytest.param([r"readiness", r"503"], id="readiness reports failure"),
    pytest.param([r"in-flight"], id="in-flight requests complete"),
    pytest.param([r"database connections"], id="connections closed"),
    pytest.param([r"exec"], id="no shell between platform and server"),
    pytest.param([r"grace period|graceful_timeout"], id="the timeout ordering"),
]


@pytest.mark.parametrize("patterns", SHUTDOWN_SEQUENCE)
def test_the_shutdown_section_names_each_step(patterns):
    """M6-05 criterion 5. Each step named, not a general "it shuts down cleanly".

    A reader who has not personally lost requests to a deploy does not know
    which steps exist, so a summary gives them nothing to check their own
    configuration against.
    """
    text = OPS_DOC.read_text()
    section = text[text.index("## Shutting down without dropping requests") :]
    section = section[: section.index("\n## What this does not solve")]

    for pattern in patterns:
        assert re.search(pattern, section, re.IGNORECASE), (
            f"the shutdown section no longer names {pattern!r}"
        )


def test_the_shutdown_docs_keep_liveness_and_readiness_apart():
    """The expensive mistake, and the one a tightening pass would cut as obvious.

    Failing liveness during a drain makes the platform KILL a process that was
    shutting down cleanly — the dropped requests arriving by way of the
    mechanism meant to prevent them.
    """
    text = OPS_DOC.read_text()

    assert re.search(r"[Ll]iveness stays 200|liveness must (keep|not)", text), (
        "docs/ops.md no longer says liveness stays up during a drain"
    )


def test_makefile_exposes_the_shutdown_demonstration():
    """M6-05 criterion 2 is verified by demonstration, so the demonstration has
    to be discoverable — `make help` lists only targets carrying a `##` comment.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert re.search(r"^shutdown-demo:.*##", makefile, re.MULTILINE), (
        "make shutdown-demo must exist and carry a ## comment so `make help` lists it"
    )
