"""Per-channel, versioned, human-owned knowledge base.

Agents read only approved versions and may submit drafts. Approval happens only in the approval
hub (ytcore/approvals/service) and the database refuses any approver that is not an active
human hub user with the approver role.
"""

from ytcore.knowledge.models import KnowledgeDoc, KnowledgeKind, KnowledgeSnapshot

__all__ = ["KnowledgeDoc", "KnowledgeKind", "KnowledgeSnapshot"]
