"""Research reports and extracted facts, with evidence references and deterministic status.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE research_reports (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        version_id uuid NOT NULL REFERENCES document_versions(id),
        category text NOT NULL,
        company text,
        model text NOT NULL,
        prompt_version text NOT NULL,
        status text NOT NULL CHECK (status IN (
            'COMPLETED', 'REJECTED_INVALID', 'FAILED', 'DEFERRED')),
        reason text,
        created_at timestamptz NOT NULL,
        available_at timestamptz,
        prompt_tokens integer,
        completion_tokens integer,
        findings_json jsonb,
        CHECK ((status = 'COMPLETED') = (available_at IS NOT NULL)),
        -- one analysis per content, model and prompt version: reruns happen only on a new key
        UNIQUE (version_id, model, prompt_version)
    )
    """,
    """
    CREATE TABLE research_facts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        report_id uuid NOT NULL REFERENCES research_reports(id),
        version_id uuid NOT NULL REFERENCES document_versions(id),
        company text,
        fact_key text NOT NULL,
        value numeric(24,6) NOT NULL,
        unit text NOT NULL,
        period text NOT NULL,
        basis text NOT NULL CHECK (basis IN ('GAAP', 'NON_GAAP', 'UNSTATED')),
        page integer NOT NULL,
        span_start integer NOT NULL,
        span_end integer NOT NULL
    )
    """,
    "CREATE INDEX research_facts_lookup_idx ON research_facts (company, fact_key, unit, basis)",
    """
    CREATE TRIGGER research_reports_immutable BEFORE UPDATE OR DELETE ON research_reports
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
    """
    CREATE TRIGGER research_facts_immutable BEFORE UPDATE OR DELETE ON research_facts
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research_facts")
    op.execute("DROP TABLE IF EXISTS research_reports")
