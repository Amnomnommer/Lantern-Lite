"""Gate 6 public-web retrieval safety and owner-launched gateway components.

Live retrieval is limited to exact owner-selected public HTTPS URLs. Retrieved
content remains untrusted, review-only evidence and is never imported
automatically.
"""

from .content import SanitizedDocument, sanitize_document
from .evidence import (
    EVIDENCE_ROLES,
    EvidenceAssessment,
    EvidenceRoleGroup,
    RetrievedClaim,
    assess_claims,
)
from .gateway import (
    retrieve_public_documents,
    validate_public_https_url,
    write_evidence_package,
)
from .request import OfflineResearchQueue, ResearchRequest, prepare_request

__all__ = [
    "EVIDENCE_ROLES",
    "EvidenceAssessment",
    "EvidenceRoleGroup",
    "OfflineResearchQueue",
    "ResearchRequest",
    "RetrievedClaim",
    "SanitizedDocument",
    "assess_claims",
    "prepare_request",
    "retrieve_public_documents",
    "sanitize_document",
    "validate_public_https_url",
    "write_evidence_package",
]
