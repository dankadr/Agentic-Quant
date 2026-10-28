"""Research documents: content-addressed versions, extracted pages, full-text search.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_STATEMENTS = [
    """
    CREATE TABLE research_documents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        logical_key text NOT NULL UNIQUE,
        category text NOT NULL CHECK (category IN (
            'FINANCIAL_STATEMENT', 'ANALYST_REPORT', 'MACRO_RELEASE', 'MANAGEMENT_CALL')),
        company text,
        title text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE document_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        document_id uuid NOT NULL REFERENCES research_documents(id),
        version_no integer NOT NULL CHECK (version_no >= 1),
        sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        byte_size integer NOT NULL CHECK (byte_size >= 0),
        content_type text NOT NULL,
        origin text NOT NULL CHECK (origin IN ('UPLOAD', 'URL')),
        source text NOT NULL,
        source_url text,
        published_at timestamptz,
        retrieved_at timestamptz NOT NULL,
        reporting_period text,
        access text NOT NULL CHECK (access IN ('PUBLIC', 'RESTRICTED')),
        extraction_status text NOT NULL CHECK (extraction_status IN (
            'EXTRACTED', 'OCR_REQUIRED', 'FAILED')),
        extraction_error text,
        amendment_of uuid REFERENCES document_versions(id),
        UNIQUE (document_id, version_no)
    )
    """,
    """
    CREATE TABLE document_pages (
        version_id uuid NOT NULL REFERENCES document_versions(id),
        page integer NOT NULL CHECK (page >= 1),
        text text NOT NULL,
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
        PRIMARY KEY (version_id, page)
    )
    """,
    "CREATE INDEX document_pages_tsv_idx ON document_pages USING GIN (tsv)",
    # Versions and their text are evidence: once written they never change.
    """
    CREATE TRIGGER document_versions_immutable BEFORE UPDATE OR DELETE ON document_versions
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
    """
    CREATE TRIGGER document_pages_immutable BEFORE UPDATE OR DELETE ON document_pages
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation()
    """,
]


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_pages")
    op.execute("DROP TABLE IF EXISTS document_versions")
    op.execute("DROP TABLE IF EXISTS research_documents")
