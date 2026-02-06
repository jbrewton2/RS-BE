# backend/questionnaire/parser.py
from __future__ import annotations

import re
from typing import List

from questionnaire.models import QuestionnaireQuestionModel


# A completely ASCII-safe regex.
# Bullet "•" is represented using \u2022 escape.
QUESTION_SPLIT_REGEX = re.compile(
    r"(?:^|\n)\s*(\d{1,3}\.|\-\s|\u2022\s)(.+?)(?=(?:\n\s*\d{1,3}\.)|\n\s*[-\u2022]\s|\Z)",
    re.DOTALL,
)


def parse_questions_from_text(raw_text: str) -> List[QuestionnaireQuestionModel]:
    """
    Parse questionnaire text into a list of questions.

    Behavior:
    - Prefer numbered or bulleted items (1., 2., -, \u2022).
    - If no structured bullets are found, fall back to one line = one question.
    """
    raw = (raw_text or "").strip()
    if not raw:
        return []

    matches = QUESTION_SPLIT_REGEX.findall(raw)
    questions: List[QuestionnaireQuestionModel] = []

    if matches:
        for idx, (_, body) in enumerate(matches, start=1):
            text = body.strip().replace("\r", "")
            if not text:
                continue
            questions.append(
                QuestionnaireQuestionModel(
                    id=f"q{idx}",
                    question_text=text,
                    tags=[],
                    status="low_confidence",
                )
            )
    else:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        for idx, line in enumerate(lines, start=1):
            questions.append(
                QuestionnaireQuestionModel(
                    id=f"q{idx}",
                    question_text=line,
                    tags=[],
                    status="low_confidence",
                )
            )

    return questions

