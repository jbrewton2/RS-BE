from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

# Canonical risk areas (UI-friendly names in code, not hard-coded in prompt)
RISK_AREAS = [
    "information_security",
    "privacy",
    "personnel_security",
    "physical_security",
    "finance",
    "project_level",
    "enterprise_level",
    "legal_data_rights",
]

# Flag/heuristic keyword triggers -> risk areas.
# NOTE: This is intentionally heuristic-ish mapping, but the trigger itself is deterministic.
# We are NOT creating risks here, only deciding which targeted questions to ask.
TRIGGER_KEYWORDS: Dict[str, Set[str]] = {
    # Information Security / CUI / DFARS / reporting / incident / encryption
    "information_security": {
        "dfars", "7012", "cui", "cdi", "incident", "report", "cyber", "security",
        "encryption", "rmf", "nist", "800-171", "800-53", "fedramp", "ato", "hosting",
        "access", "audit", "log", "siem", "vulnerability", "scan", "stigs", "cmmc",
    },
    # Privacy / PII / PHI
    "privacy": {
        "pii", "phi", "privacy", "hipaa", "privacy act", "gdpr", "consent", "breach",
        "data subject", "personal information",
    },
    # Personnel / citizenship / clearance / background checks
    "personnel_security": {
        "clearance", "secret", "top secret", "ts/sci", "citizen", "citizenship",
        "background", "fingerprint", "suitability", "public trust",
    },
    # Physical / facility / scif / access badges
    "physical_security": {
        "scif", "facility", "badge", "physical", "secure area", "controlled area",
        "onsite", "on-site", "visit", "escort",
    },
    # Finance / pricing / payment / CLINs
    "finance": {
        "pricing", "price", "payment", "invoice", "clin", "cost", "fee",
        "firm-fixed-price", "ffp", "t&m", "time and materials",
    },
    # Project-level execution risk (deliverables, acceptance, schedule)
    "project_level": {
        "deliverable", "acceptance", "milestone", "schedule", "timeline", "pop",
        "period of performance", "slas", "requirements", "test event",
    },
    # Enterprise-level (flow-downs, policies, governance)
    "enterprise_level": {
        "flowdown", "flow-down", "subcontract", "teaming", "prime", "audit rights",
        "records", "compliance", "governance",
    },
    # Legal / data rights / IP
    "legal_data_rights": {
        "data rights", "rights in data", "government purpose rights", "limited rights",
        "unlimited rights", "ip", "intellectual property", "license", "indemnification",
        "termination", "dispute", "jurisdiction",
    },
}

# Targeted questions per area (bounded by caller).
TARGETED_QUESTIONS: Dict[str, List[str]] = {
    "information_security": [
        "Identify any explicit cybersecurity, CUI/CDI handling, incident reporting, or NIST/DFARS compliance requirements. Quote the relevant language.",
        "Identify any hosting/environment constraints (GovCloud, on-prem, FedRAMP, RMF/ATO, network restrictions). Quote the relevant language.",
    ],
    "privacy": [
        "Identify any privacy/PII/PHI handling requirements, breach notification, consent, or privacy act language. Quote the relevant language.",
        "Identify any data retention, access control, or disclosure constraints tied to personal data. Quote the relevant language.",
    ],
    "personnel_security": [
        "Identify any personnel clearance, citizenship, background checks, or access requirements. Quote the relevant language.",
        "Identify any staffing constraints that could impact delivery (on-site, escorted access, key personnel). Quote the relevant language.",
    ],
    "physical_security": [
        "Identify any physical security, facility, SCIF, controlled area, or on-site access requirements. Quote the relevant language.",
        "Identify any delivery constraints driven by physical access (escorts, badging, visits, restricted areas). Quote the relevant language.",
    ],
    "finance": [
        "Identify any pricing structure, contract type, CLIN structure, payment terms, or invoice requirements. Quote the relevant language.",
        "Identify any cost risk drivers (undefined scope, undefined acceptance, optional CLINs) and quote the triggering language.",
    ],
    "project_level": [
        "Identify deliverables, acceptance criteria, and schedule/timeline requirements. Quote the relevant language.",
        "Identify any test event phases, success criteria, or support obligations that could create schedule risk. Quote the relevant language.",
    ],
    "enterprise_level": [
        "Identify any flow-downs, subcontracting, teaming, audit rights, or governance constraints. Quote the relevant language.",
        "Identify any compliance or reporting obligations that create enterprise-level burden. Quote the relevant language.",
    ],
    "legal_data_rights": [
        "Identify any data rights / IP / licensing / reuse constraints. Quote the relevant language.",
        "Identify any termination, dispute, indemnification, or liability clauses that increase legal risk. Quote the relevant language.",
    ],
}

def detect_triggered_areas_from_signals(
    flag_hits: List[dict] | None,
    heuristic_hits: List[dict] | None,
) -> Set[str]:
    """
    Deterministically decide which risk areas should get targeted retrieval questions.
    We look at flag ids/labels and heuristic ids/labels/snippets for keyword matches.
    """
    text_blobs: List[str] = []

    for h in (flag_hits or []):
        text_blobs.append(str(h.get("id", "")).lower())
        text_blobs.append(str(h.get("label", "")).lower())
        text_blobs.append(str(h.get("snippet", "")).lower())

    for h in (heuristic_hits or []):
        text_blobs.append(str(h.get("id", "")).lower())
        text_blobs.append(str(h.get("label", "")).lower())
        text_blobs.append(str(h.get("why", "")).lower())

    blob = " | ".join([b for b in text_blobs if b.strip()])

    triggered: Set[str] = set()
    for area, keys in TRIGGER_KEYWORDS.items():
        for k in keys:
            if k in blob:
                triggered.add(area)
                break

    return triggered

def build_targeted_questions(triggered_areas: Set[str], max_questions: int = 10) -> List[str]:
    """
    Return a bounded list of targeted questions (max_questions).
    """
    out: List[str] = []
    for area in RISK_AREAS:
        if area not in triggered_areas:
            continue
        for q in TARGETED_QUESTIONS.get(area, []):
            out.append(q)
            if len(out) >= max_questions:
                return out
    return out
