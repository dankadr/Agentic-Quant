"""Which job kinds each service role may claim.

A worker claims only the kinds assigned to its role, so a research handler can never run inside
the process that holds the executor lease, and an execution job never waits behind a model call.
Every kind the scheduler enqueues must appear in exactly one set; the unit test enforces this.
"""

EXECUTION_KINDS = frozenset({"session_start", "paper_tick", "session_end", "reconcile"})
OPERATIONS_KINDS = frozenset({"heartbeat", "outbox_dispatch"})
RESEARCH_KINDS = frozenset(
    {"agent_fundamental", "agent_consensus", "agent_macro", "agent_evaluator", "document_import"}
)
ALL_KINDS = EXECUTION_KINDS | OPERATIONS_KINDS | RESEARCH_KINDS
