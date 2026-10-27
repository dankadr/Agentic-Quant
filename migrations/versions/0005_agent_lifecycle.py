"""Agent lifecycle: availability time, attempt number, token usage.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Additive only. Existing rows keep available_at NULL: their availability cannot be verified, so
# causal evaluation excludes them. We never backfill a timestamp that was not recorded.
_STATEMENTS = [
    # A run skipped for a missing prerequisite is recorded as SKIPPED with a reason, not dropped.
    "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_status_check",
    """
    ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check CHECK (status IN (
        'RUNNING', 'COMPLETED', 'FAILED', 'REJECTED_INVALID', 'SKIPPED'))
    """,
    "ALTER TABLE agent_runs ADD COLUMN available_at timestamptz",
    "ALTER TABLE agent_runs ADD COLUMN attempt integer NOT NULL DEFAULT 1",
    "ALTER TABLE agent_runs ADD COLUMN prompt_tokens integer",
    "ALTER TABLE agent_runs ADD COLUMN completion_tokens integer",
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for column in ("completion_tokens", "prompt_tokens", "attempt", "available_at"):
        op.execute(f"ALTER TABLE agent_runs DROP COLUMN IF EXISTS {column}")
