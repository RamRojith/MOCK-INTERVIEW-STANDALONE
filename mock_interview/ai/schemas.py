RESUME_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "skills": {"type": "array", "items": {"type": "string"}},
        "projects": {"type": "array", "items": {"type": "string"}},
        "education": {"type": "array", "items": {"type": "string"}},
        "experience": {"type": "array", "items": {"type": "string"}},
        "certifications": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["skills", "projects", "education", "experience", "certifications"],
}

ATS_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "experience": {"type": "array", "items": {"type": "string"}},
        "projects": {"type": "array", "items": {"type": "string"}},
        "education": {"type": "array", "items": {"type": "string"}},
        "certifications": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "skills",
        "experience",
        "projects",
        "education",
        "certifications",
    ],
}

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question_text": {"type": "string"},
        "question_type": {"type": "string"},
        "source": {"type": "string"},
        "selection_reason": {"type": "string"},
        "expected_concepts": {"type": "array", "items": {"type": "string"}},
        "rubric": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
    },
    "required": [
        "question_text",
        "question_type",
        "source",
        "selection_reason",
        "expected_concepts",
        "rubric",
    ],
}

EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "dimension_scores": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "evidence": {"type": "array", "items": {"type": "string"}},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "missing_concepts": {"type": "array", "items": {"type": "string"}},
        "improvement_actions": {"type": "array", "items": {"type": "string"}},
        "improved_answer": {"type": "string"},
    },
    "required": [
        "dimension_scores",
        "evidence",
        "strengths",
        "missing_concepts",
        "improvement_actions",
        "improved_answer",
    ],
}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "improvement_areas": {"type": "array", "items": {"type": "string"}},
        "learning_plan": {"type": "array", "items": {"type": "string"}},
        "dimension_analysis": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["summary", "strengths", "improvement_areas", "learning_plan"],
}

# SWOT (Strengths, Weaknesses, Opportunities, Threats) for the coaching report.
# Internal axis (Strengths / Weaknesses) = competencies the student showed or
# lacked in this interview. External / forward-looking axis (Opportunities /
# Threats) = growth levers and risks for future interviews. Every item must be
# grounded in the validated evaluation data — never a hiring or employability
# verdict.
SWOT_SCHEMA = {
    "type": "object",
    "properties": {
        "strengths": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 220},
        },
        "weaknesses": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 220},
        },
        "opportunities": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 220},
        },
        "threats": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 220},
        },
    },
    "required": ["strengths", "weaknesses", "opportunities", "threats"],
}

LIVE_EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "dimension_scores": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
        "evidence": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 180},
        },
        "strengths": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 180},
        },
        "missing_concepts": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "maxLength": 120},
        },
        "improvement_actions": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 180},
        },
    },
    "required": [
        "dimension_scores",
        "evidence",
        "strengths",
        "missing_concepts",
        "improvement_actions",
    ],
}

LIVE_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question_text": {"type": "string", "maxLength": 500},
        "question_type": {"type": "string", "maxLength": 50},
        "expected_concepts": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "maxLength": 100},
        },
        "rubric": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
    },
    "required": [
        "question_text",
        "question_type",
        "expected_concepts",
        "rubric",
    ],
}

COMBINED_TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluation": LIVE_EVALUATION_SCHEMA,
        "next_question": LIVE_QUESTION_SCHEMA,
    },
    "required": ["evaluation", "next_question"],
}
