from __future__ import annotations

from typing import List, Tuple

def _question_section_map(intent: str) -> List[Tuple[str, str]]:
    intent = (intent or "strict_summary").strip().lower()

    if intent == "risk_triage":
        return [
            ("SECURITY, COMPLIANCE & HOSTING CONSTRAINTS", "Identify cybersecurity / ATO / RMF / IL requirements and risks (encryption, logging, incident reporting, vuln mgmt)."),
            ("SECURITY, COMPLIANCE & HOSTING CONSTRAINTS", "Identify CUI handling / safeguarding requirements and risks (marking, access, transmission, storage, disposal)."),
            ("LEGAL & DATA RIGHTS RISKS", "Identify privacy / PII / data protection obligations and risks."),
            ("LEGAL & DATA RIGHTS RISKS", "Identify legal/data-rights terms and risks (IP/data rights, audit rights, GFI/GFM handling, disclosure penalties)."),
            ("ELIGIBILITY & PERSONNEL CONSTRAINTS", "Identify subcontractor / flowdown / staffing constraints and risks (citizenship, clearance, facility, export)."),
            ("DELIVERABLES & TIMELINES", "Identify delivery/acceptance gates and required approvals (CDRLs, QA, test, acceptance criteria)."),
            ("FINANCIAL RISKS", "Identify financial and invoicing risks (ceilings, overruns, payment terms, reporting cadence)."),
            ("DELIVERABLES & TIMELINES", "Identify schedule risks (IMS, milestones, reporting cadence, penalties)."),
            ("CONTRADICTIONS & INCONSISTENCIES", "Identify ambiguous/undefined terms and contradictions that require clarification."),
            ("OVERVIEW", "List top red-flag phrases/requirements with evidence and suggested internal owner (security/legal/PM/finance)."),
            ("MISSION & OBJECTIVE", "What is the mission and objective of this effort?"),
            ("SCOPE OF WORK", "What is the scope of work and required deliverables?"),
            ("SUBMISSION INSTRUCTIONS & DEADLINES", "What are submission instructions and deadlines, including required formats and delivery method?"),
            ("GAPS / QUESTIONS FOR THE GOVERNMENT", "What gaps require clarification from the Government?"),
            ("RECOMMENDED INTERNAL ACTIONS", "What internal actions should we take next (security/legal/PM/engineering/finance)?"),
        ]

    return [
        ("MISSION & OBJECTIVE", "What is the mission and objective of this effort?"),
        ("SCOPE OF WORK", "What is the scope of work and required deliverables?"),
        ("SECURITY, COMPLIANCE & HOSTING CONSTRAINTS", "What are the security, compliance, and hosting constraints (IL levels, NIST, DFARS, CUI, ATO/RMF, logging)?"),
        ("ELIGIBILITY & PERSONNEL CONSTRAINTS", "What are the eligibility and personnel constraints (citizenship, clearances, facility, location, export controls)?"),
        ("LEGAL & DATA RIGHTS RISKS", "What are key legal and data rights risks (IP/data rights, audit rights, flowdowns)?"),
        ("FINANCIAL RISKS", "What are key financial risks (pricing model, ceilings, invoicing systems, payment terms)?"),
        ("SUBMISSION INSTRUCTIONS & DEADLINES", "What are submission instructions and deadlines, including required formats and delivery method?"),
        ("CONTRADICTIONS & INCONSISTENCIES", "What contradictions or inconsistencies exist across documents?"),
        ("GAPS / QUESTIONS FOR THE GOVERNMENT", "What gaps require clarification from the Government?"),
        ("RECOMMENDED INTERNAL ACTIONS", "What internal actions should we take next (security/legal/PM/engineering/finance)?"),
    ]

