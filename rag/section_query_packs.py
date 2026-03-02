from __future__ import annotations

from typing import Dict, List

# Keyed by section_id used in API output (slug ids like "submission-instructions-deadlines").
# Each pack should be 2-4 queries, phrased differently to reduce wording mismatch.
SECTION_QUERY_PACKS: Dict[str, List[str]] = {
    "overview": [
        "overview summary key requirements shall must prohibited",
        "primary objectives constraints risks assumptions scope",
    ],
    "mission-objective": [
        "mission objective purpose goals outcomes end state",
        "statement of objectives SOO mission context",
    ],
    "scope-of-work": [
        "scope of work tasks responsibilities contractor shall provide perform",
        "technical requirements functional requirements deliver services",
    ],
    "deliverables-timelines": [
        "deliverables milestones schedule IMS due dates calendar days no later than",
        "CDRL DRL DID data item description deliverable A0 A00 submission",
        "acceptance criteria government acceptance review approve reject",
    ],
    "security-compliance-hosting-constraints": [
        "IL5 IL4 SRG RMF ATO authority to operate compliance controls",
        "encryption logging audit monitoring incident reporting vulnerability scanning SBOM",
        "prohibited actions no docker hub no public registry no outbound egress",
    ],
    "eligibility-personnel-constraints": [
        "citizenship clearance background check CAC PIV personnel requirements",
        "staffing labor categories key personnel qualifications onboarding timeline",
    ],
    "legal-data-rights-risks": [
        "data rights IP intellectual property unlimited rights restricted rights",
        "audit rights records access government furnished information GFI GFM",
        "flowdown subcontractor clauses nondisclosure disclosure penalties",
    ],
    "financial-risks": [
        "pricing CLIN invoice invoicing payment terms acceptance for payment",
        "ceilings overruns cost allowability reporting cadence burn rate",
        "period of performance option periods funding incremental funding",
    ],
    "submission-instructions-deadlines": [
        "Section L instructions to offerors submission format volumes page limits",
        "proposal submission due date time portal email address deliver method",
        "SF1449 submit signed and dated offers deadline",
    ],
    "contradictions-inconsistencies": [
        "contradiction inconsistency conflict ambiguous undefined terms",
        "order of precedence in the event of conflict contract controls",
        "shall must vs may permissive conflicting requirements",
    ],
    "gaps-questions-for-the-government": [
        "to be determined TBD government will provide clarification missing information",
        "not specified requires clarification ambiguous requirement",
        "questions for the government request clarification",
    ],
    "recommended-internal-actions": [
        "recommended actions internal next steps compliance plan risk mitigation",
        "implementation plan responsibilities owners security legal program finance",
    ],
}
