"""email verification + password reset: users columns and email_tokens

Revision ID: 0003_email_tokens
Revises: 0002_screen_snapshots
Create Date: 2026-08-20

Adds:
  * users.email_verified_at    — NULL until the address is confirmed
  * users.password_changed_at  — audit trail for the last password change
  * users.token_version        — JWTs carrying a different `ver` are rejected
  * email_tokens               — single-use, hashed, expiring links

Existing rows are backfilled deliberately:
  * email_verified_at stays NULL, so accounts created before verification
    existed are shown the "please verify" banner rather than being silently
    grandfathered in as verified.
  * password_changed_at is seeded from created_at (not now()) — it is only an
    audit column, and stamping every row with the deploy time would erase real
    history.
  * token_version starts at 0 for everyone. Note that tokens minted by the
    previous build carry no `ver` claim at all and are rejected, so this deploy
    signs every active session out once. That is the intended trade: silently
    accepting version-less tokens would leave a window in which a password
    reset does not actually revoke anything.

Emails are also normalised to lower case here, to match crud.normalize_email().
If a database already contains two rows differing only by case, the unique
index will refuse the update — that collision needs a human decision about
which account survives, so failing loudly is correct.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_email_tokens"
down_revision: Union[str, None] = "0002_screen_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    # Seed from created_at so the audit column reflects reality, then tighten.
    op.execute("UPDATE users SET password_changed_at = created_at")
    op.alter_column("users", "password_changed_at", nullable=False)

    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.execute("UPDATE users SET email = lower(email)")

    op.create_table(
        "email_tokens",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique: the lookup path is "find the row for this hash", and a duplicate
    # would mean one link addressing two accounts.
    op.create_index(
        "ix_email_tokens_token_hash", "email_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_email_tokens_user_id", "email_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_email_tokens_user_id", table_name="email_tokens")
    op.drop_index("ix_email_tokens_token_hash", table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_column("users", "token_version")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "email_verified_at")
