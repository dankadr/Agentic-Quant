# Research documents: intake, storage, and citations

## What is stored

- **Originals** are written once, under their SHA-256, in `${TRADING_DATA_DIR:-./data}/documents` on the host
  (`/app/data/documents`). Identical bytes are stored once. A stored file is never rewritten, and a
  read verifies its checksum.
- **Versions and pages** live in PostgreSQL. They are append-only: database triggers refuse any
  update or delete. A citation resolves against a version's own stored text.

## Intake

- **Upload** (`POST /v1/research/documents`, operator). Content is base64 in JSON. The declared type
  must equal the type the bytes actually have: a PDF labelled as HTML is refused, not guessed at.
  Types accepted: PDF, HTML, plain text. Size is capped by `configs/documents.yaml`.
- **URL import** (`POST /v1/research/import-url`, operator). Only hosts listed in
  `configs/documents.yaml` are allowed, and only over https. The request queues a job; the research
  worker fetches it and follows each redirect by hand, checking the host and resolved addresses
  again at every hop. Private, loopback, and link-local addresses are refused.
- Approving a source is a config change: add the host to `allowed_hosts`, commit, and redeploy.

## Processing

- Parsing runs in a child process started with an empty environment and `python -I`, under a time
  limit. A hostile or corrupt file becomes a `FAILED` version with its error recorded.
- HTML scripts, styles, frames, and embedded objects are removed before text is kept.
- A PDF with no text layer is recorded as `OCR_REQUIRED`. OCR is out of scope for this phase.

## Time rules

- `published_at` is what the source says. `retrieved_at` is when this system obtained the bytes.
  Neither is back-filled or inferred.
- A changed document with the same identity is a new version (`version_no` increases and
  `amendment_of` points at the version it amends). Earlier versions are kept.

## Known limits

- The network layer does not pin resolved addresses to the connection. Egress filtering at the host
  is the backstop for DNS rebinding.
- The child process is isolated by environment and mode, not by a container or seccomp profile.
