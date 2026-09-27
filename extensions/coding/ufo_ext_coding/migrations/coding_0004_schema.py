"""The coding extension holds no tables of its own; a database at this revision owes it nothing.

The revision stays because deployed databases are stamped `coding_0004`, and alembic resolves every
stamped head from the script directory before it runs any DDL — a head whose file left the tree
stops the migrate Job the deploy waits on.
"""

revision: str = "coding_0004"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("coding",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    """Nothing to build."""


def downgrade() -> None:
    """Nothing to tear down."""
