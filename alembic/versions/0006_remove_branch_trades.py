"""remove the retired branch trades table"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006_remove_branch_trades"
down_revision: Union[str, None] = "0005_branch_trades"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep 0005 in the migration chain so databases already stamped at that
    # revision can upgrade cleanly, then remove the retired feature's table.
    op.execute("DROP TABLE IF EXISTS branch_trades CASCADE")


def downgrade() -> None:
    # The feature is retired; restoring its schema is intentionally omitted.
    pass
