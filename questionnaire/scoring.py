# backend/questionnaire/scoring.py
from __future__ import annotations
from typing import List, Optional
from questionnaire.models import QuestionnaireQuestionModel

def derive_status_and_confidence(questions: List[QuestionnaireQuestionModel]) -> Optional[float]:
    confidences = []

    for q in questions:
        c = q.confidence or 0.0

        if q.answer_source == "bank":
            q.status = "auto_approved"
            if c == 0:
                c = 0.9

        else:
            if c >= 0.75:
                q.status = "needs_review"
            else:
                q.status = "low_confidence"

        q.confidence = c
        if c > 0:
            confidences.append(c)

    if not confidences:
        return None

    return sum(confidences) / len(confidences)

