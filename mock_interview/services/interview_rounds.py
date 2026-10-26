"""Canonical interview-round definitions shared by every question path.

The setup dropdown stores six human-readable round labels. Question generation
used to pass that label to the model without explaining what it meant, and the
deterministic fallback only recognised HR/behavioural. This module is the single
source of truth for:

* normalising labels and common aliases,
* the focus and constraints for each round,
* the controlled category order used by Mixed interviews,
* the prompt block appended to the planner/interviewer system prompt,
* round-specific deterministic questions used when the local model is offline,
* validation that an LLM plan actually matches the selected round.

Keeping this logic here means the fast pre-planned pipeline and the legacy
per-turn pipeline cannot drift apart.
"""

from __future__ import annotations

from collections import Counter

TECHNICAL = "Technical"
HR = "HR"
BEHAVIOURAL = "Behavioural"
PROJECT = "Project Discussion"
MANAGERIAL = "Managerial"
MIXED = "Mixed"

ROUND_LABELS = (TECHNICAL, HR, BEHAVIOURAL, PROJECT, MANAGERIAL, MIXED)

TECHNICAL_RUBRIC = {
    "technical_correctness": 40,
    "completeness": 20,
    "relevance": 15,
    "structure": 15,
    "practical_example": 10,
}

BEHAVIOURAL_RUBRIC = {
    "relevance": 20,
    "star_structure": 30,
    "specific_evidence": 20,
    "reflection": 15,
    "communication": 15,
}

# Mixed uses the rubric that belongs to each question's category.
TECHNICAL_FAMILY = ("technical", "project", "managerial")
BEHAVIOURAL_FAMILY = ("behavioural", "hr")

# A controlled Mixed interview, rather than "ask anything". One cycle covers
# all five categories and repeats only when more questions are requested.
MIXED_CYCLE = ("technical", "project", "behavioural", "hr", "managerial")

ROUND_ALIASES = {
    "technical": TECHNICAL,
    "tech": TECHNICAL,
    "technical round": TECHNICAL,
    "hr": HR,
    "human resources": HR,
    "hr round": HR,
    "behavioural": BEHAVIOURAL,
    "behavioral": BEHAVIOURAL,
    "behaviour": BEHAVIOURAL,
    "behavior": BEHAVIOURAL,
    "behavioural round": BEHAVIOURAL,
    "project discussion": PROJECT,
    "project": PROJECT,
    "projects": PROJECT,
    "project round": PROJECT,
    "managerial": MANAGERIAL,
    "manager": MANAGERIAL,
    "management": MANAGERIAL,
    "managerial round": MANAGERIAL,
    "mixed": MIXED,
    "general": MIXED,
    "": MIXED,
}

ROUND_PROFILES = {
    TECHNICAL: {
        "label": TECHNICAL,
        "question_type": "technical",
        "focus": (
            "technical knowledge, coding, algorithms, data structures, "
            "debugging, system design, and role-specific concepts"
        ),
        "instructions": (
            "Every question must assess technical knowledge, coding, "
            "algorithms, or role-specific concepts.",
            "Ask the candidate to explain, design, debug, compare, or "
            "implement something concrete.",
            "Do not ask motivation, career-goal, behavioural STAR, project "
            "history, or managerial questions in this round.",
        ),
        "rubric": TECHNICAL_RUBRIC,
    },
    HR: {
        "label": HR,
        "question_type": "hr",
        "focus": (
            "motivation, career goals, communication, professionalism, and "
            "organizational fit"
        ),
        "instructions": (
            "Every question must assess motivation, career goals, "
            "communication, professionalism, or organizational fit.",
            "Keep the questions conversational and connected to the target "
            "role, company style, and the candidate's background.",
            "Do not ask coding, algorithm, system-design, project-architecture, "
            "or managerial resource-planning questions in this round.",
        ),
        "rubric": BEHAVIOURAL_RUBRIC,
    },
    BEHAVIOURAL: {
        "label": BEHAVIOURAL,
        "question_type": "behavioural",
        "focus": (
            "past experiences, teamwork, leadership, ownership, conflict "
            "resolution, and learning from outcomes"
        ),
        "instructions": (
            "Every question must ask for a specific past experience, not a "
            "hypothetical or purely technical explanation.",
            "Probe teamwork, leadership, conflict resolution, ownership, "
            "failure, or learning, and encourage a STAR-style answer.",
            "Do not ask coding puzzles, definitions, or generic HR motivation "
            "questions in this round.",
        ),
        "rubric": BEHAVIOURAL_RUBRIC,
    },
    PROJECT: {
        "label": PROJECT,
        "question_type": "project",
        "focus": (
            "the candidate's actual projects, architecture, technology "
            "choices, challenges, contributions, and outcomes"
        ),
        "instructions": (
            "Every question must probe a project the candidate actually worked "
            "on, using the resume project details when available.",
            "Ask about architecture, technology choices, trade-offs, the "
            "hardest challenge, the candidate's specific contribution, and "
            "measurable outcomes.",
            "Do not ask generic HR, standalone algorithm puzzles, or "
            "managerial resource-planning questions in this round.",
        ),
        "rubric": TECHNICAL_RUBRIC,
    },
    MANAGERIAL: {
        "label": MANAGERIAL,
        "question_type": "managerial",
        "focus": (
            "decision-making, prioritization, leadership, delegation, "
            "stakeholder communication, and resource management"
        ),
        "instructions": (
            "Every question must assess decision-making, prioritization, "
            "leadership, delegation, stakeholder communication, or resource "
            "management.",
            "Use realistic role-based scenarios about competing deadlines, "
            "limited people or time, underperformance, and trade-offs.",
            "Do not ask coding exercises, definitions, or generic HR "
            "motivation questions in this round.",
        ),
        "rubric": TECHNICAL_RUBRIC,
    },
    MIXED: {
        "label": MIXED,
        "question_type": "mixed",
        "focus": (
            "a controlled combination of technical, project, behavioural, HR, "
            "and managerial questions"
        ),
        "instructions": (
            "Generate a controlled combination of questions from the "
            "categories assigned below; do not let one category dominate.",
            "Follow the exact category order and assign each question the "
            "listed type.",
            "Every question must still be specific to the candidate's resume, "
            "target role, skills, and job description when available.",
        ),
        "rubric": TECHNICAL_RUBRIC,
    },
}


def normalize_interview_round(value):
    """Return the canonical label for a stored/selected round.

    Unknown or empty values fall back to Mixed, which is the safe behaviour for
    older sessions that predate the six-round dropdown.
    """
    text = " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )
    return ROUND_ALIASES.get(text, MIXED)


def round_profile(value):
    return ROUND_PROFILES[normalize_interview_round(value)]


def canonical_question_type(value):
    text = " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )
    aliases = {
        "behavioural": "behavioural",
        "behavioral": "behavioural",
        "behavior": "behavioural",
        "behaviour": "behavioural",
        "project discussion": "project",
        "project": "project",
        "projects": "project",
        "managerial": "managerial",
        "management": "managerial",
        "manager": "managerial",
        "hr": "hr",
        "human resources": "hr",
        "technical": "technical",
        "tech": "technical",
        "situational": "situational",
    }
    return aliases.get(text, text)


def categories_for_count(round_value, question_count):
    """Return the exact category for each question in the session."""
    count = max(0, int(question_count or 0))
    label = normalize_interview_round(round_value)
    if label == MIXED:
        return tuple(MIXED_CYCLE[index % len(MIXED_CYCLE)] for index in range(count))
    category = ROUND_PROFILES[label]["question_type"]
    return (category,) * count


def category_for_question(round_value, sequence_number, question_count):
    """Category assigned to one question (1-based sequence number)."""
    seq = max(1, int(sequence_number or 1))
    count = max(seq, int(question_count or seq))
    categories = categories_for_count(round_value, count)
    return categories[(seq - 1) % len(categories)]


def round_prompt_block(round_value, question_count):
    """System-prompt text that makes the selected round unambiguous."""
    label = normalize_interview_round(round_value)
    profile = ROUND_PROFILES[label]
    count = max(1, int(question_count or 1))
    categories = categories_for_count(label, count)

    lines = [
        f"INTERVIEW ROUND: {label}",
        f"ROUND FOCUS: {profile['focus']}",
        "ROUND-SPECIFIC REQUIREMENTS:",
    ]
    lines.extend(f"- {instruction}" for instruction in profile["instructions"])

    if label == MIXED:
        counts = Counter(categories)
        distribution = ", ".join(
            f"{counts[category]} {category}"
            for category in MIXED_CYCLE
            if counts.get(category)
        )
        lines.append(f"CONTROLLED MIX for this session: {distribution}.")
        lines.append(
            "Use this exact category order: "
            + " -> ".join(categories)
            + "."
        )
        lines.append(
            "Set each question's `type` field to its assigned category "
            "(technical, project, behavioural, hr, or managerial)."
        )
    else:
        lines.append(
            f"Every question must set `type` to `{profile['question_type']}`."
        )
        lines.append(
            "Do not drift into another interview-round category."
        )

    lines.append(
        "Use the candidate's resume, job description, target skills, target "
        "role, and company style when that context is available."
    )
    return "\n".join(lines)


def plan_matches_round(round_value, items, question_count):
    """Return (matches, detail) for an LLM plan against the round contract."""
    expected = categories_for_count(round_value, question_count)
    for index, item in enumerate(list(items or [])[: len(expected)], start=1):
        actual = canonical_question_type(
            item.get("question_type") or item.get("type") or ""
        )
        required = expected[index - 1]
        if actual != required:
            return (
                False,
                f"Q{index} expected type '{required}' but model returned "
                f"'{actual or 'missing'}'",
            )
    return True, ""


def rubric_for_category(category):
    if canonical_question_type(category) in BEHAVIOURAL_FAMILY:
        return dict(BEHAVIOURAL_RUBRIC)
    return dict(TECHNICAL_RUBRIC)


def _short(value, limit=120):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _session_skills(session, limit=8):
    skills = list(getattr(session, "target_skills", None) or [])
    if not skills and getattr(session, "resume_id", None):
        profile = getattr(getattr(session, "resume", None), "structured_profile", None) or {}
        skills = profile.get("skills") or profile.get("technical_skills") or []
    if not skills and getattr(session, "mock_interview_id", None):
        mock = getattr(session, "mock_interview", None)
        skills = list(getattr(mock, "target_skills", None) or [])
    cleaned = []
    for item in skills:
        text = _short(item, 80)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:limit]


def _session_project(session):
    if not getattr(session, "resume_id", None):
        return ""
    profile = getattr(getattr(session, "resume", None), "structured_profile", None) or {}
    projects = (
        profile.get("projects")
        or profile.get("project_highlights")
        or profile.get("experience")
        or []
    )
    if isinstance(projects, str):
        projects = [projects]
    for project in projects:
        text = _short(project, 120)
        if text:
            return text
    return ""


def _role(session):
    return _short(getattr(session, "role", "") or "target role", 80)


def _company(session):
    return _short(getattr(session, "company_name", "") or "", 80)


def _with_article(phrase):
    """Use 'an' for vowel-sounding labels such as AI Engineer or SQL."""
    text = str(phrase or "").strip()
    if not text:
        return text
    return f"an {text}" if text[0].lower() in "aeiou" else f"a {text}"


def _first_skill(session, fallback):
    skills = _session_skills(session, limit=1)
    return skills[0] if skills else fallback


def _question_template(session, category, sequence_number, variant):
    role = _role(session)
    role_ref = _with_article(role)
    skills = _session_skills(session)
    skill = skills[(sequence_number - 1) % len(skills)] if skills else role
    skill_ref = _with_article(skill)
    project = _session_project(session)
    project_ref = (
        f"your project '{project}'"
        if project
        else f"a project relevant to the {role} role"
    )
    company = _company(session)
    company_phrase = f" at a company like {company}" if company else ""

    templates = {
        "technical": [
            (
                f"Walk me through how you would use {skill} to solve a "
                f"practical {role} problem. Explain your approach, the "
                "trade-offs, and how you would test the result."
            ),
            (
                f"Explain a core {skill} concept that {role_ref} should know, "
                "then give a short implementation example."
            ),
            (
                f"Describe how you would debug a difficult issue involving "
                f"{skill} in {role_ref} project. What steps and tools would "
                "you use?"
            ),
            (
                f"How would you design a small {role} feature with {skill}? "
                "Which data structures or algorithms would you choose, and "
                "why?"
            ),
            (
                f"Compare two ways to implement {skill_ref} feature for "
                f"{role_ref} application. What trade-offs would drive your "
                "decision?"
            ),
        ],
        "hr": [
            (
                f"Why are you interested in the {role} role{company_phrase}, "
                "and what motivates you to pursue this career path?"
            ),
            (
                f"Where do you see your career in the next three years, and "
                f"how does this {role} opportunity fit those goals?"
            ),
            (
                f"Tell me about a time you explained a complex idea to someone "
                f"outside your team on {role_ref} project. How did you make it "
                "clear?"
            ),
            (
                "What kind of team culture and work environment help you "
                f"communicate and perform at your best as {role_ref}?"
            ),
            (
                f"What is one strength and one weakness that would affect how "
                f"you fit into this {role} role?"
            ),
        ],
        "behavioural": [
            (
                f"Tell me about a time you worked with a team to deliver "
                f"{role_ref} project. What was your contribution, and what "
                "was the outcome?"
            ),
            (
                f"Describe a disagreement you had with a teammate on "
                f"{role_ref} project. How did you resolve it, and what did "
                "you learn?"
            ),
            (
                f"Tell me about a time you took ownership of a difficult "
                f"{role} problem. What actions did you take, and what changed "
                "as a result?"
            ),
            (
                f"Describe a situation where you had to coordinate or lead "
                f"others on {role_ref} task. How did you approach it, and what "
                "was the result?"
            ),
            (
                f"Tell me about a setback or failure in {role_ref} project. "
                "What happened, and what would you do differently now?"
            ),
        ],
        "project": [
            (
                f"Walk me through {project_ref}. What problem did it solve, "
                "and what was your specific contribution?"
            ),
            (
                f"In {project_ref}, what architecture did you choose, and why "
                "did that structure fit the problem?"
            ),
            (
                f"Which technology choices in {project_ref} were most "
                "important? What trade-offs did you consider?"
            ),
            (
                f"What was the hardest technical challenge in {project_ref}, "
                "and how did you diagnose and resolve it?"
            ),
            (
                f"What measurable outcome did {project_ref} achieve, and what "
                "would you improve if you rebuilt it?"
            ),
        ],
        "managerial": [
            (
                f"How would you prioritize competing deadlines for {role_ref} "
                "team when every task is urgent? Walk me through your "
                "decision process."
            ),
            (
                f"Tell me about a time you made a decision with incomplete "
                f"information on {role_ref} task. How did you evaluate the "
                "options and manage the outcome?"
            ),
            (
                f"How would you allocate limited people and time across two "
                f"important {role} projects? What trade-offs would you make?"
            ),
            (
                f"Describe how you would handle an underperforming teammate "
                f"on {role_ref} project while keeping the work on track."
            ),
            (
                f"How would you communicate a difficult {role} trade-off or "
                "delay to stakeholders, and what would you propose?"
            ),
        ],
    }
    choices = templates[category]
    return choices[(int(sequence_number) - 1 + int(variant)) % len(choices)]


def _selection_reason(session, category, sequence_number):
    profile = round_profile(getattr(session, "interview_round", ""))
    reason = {
        "technical": "probes applied technical knowledge, design, and testing",
        "hr": "probes motivation, communication, and organizational fit",
        "behavioural": "probes a specific past experience using a STAR-style prompt",
        "project": "probes the candidate's own project architecture and contribution",
        "managerial": "probes prioritization, leadership, and resource decisions",
    }[category]
    return (
        f"{profile['label']} round, question {sequence_number}: {reason}. "
        "Generated deterministically because the local model was unavailable."
    )


def _expected_concepts(session, category, sequence_number):
    role = _role(session)
    skill = _first_skill(session, role)
    if category == "technical":
        return [skill, role]
    if category == "project":
        project = _session_project(session)
        return [project, role] if project else [role, "project architecture"]
    if category == "hr":
        return ["motivation", "career goals", "communication", "organizational fit"]
    if category == "behavioural":
        return [
            "teamwork",
            "ownership",
            "conflict resolution",
            "leadership",
        ]
    return [
        "decision making",
        "prioritization",
        "leadership",
        "resource management",
    ]


def deterministic_round_question(session, sequence_number, *, variant=0):
    """Build one round-appropriate question without calling an LLM."""
    seq = max(1, int(sequence_number or 1))
    count = max(seq, int(getattr(session, "question_count", seq) or seq))
    category = category_for_question(
        getattr(session, "interview_round", ""),
        seq,
        count,
    )
    topic = {
        "technical": _first_skill(session, _role(session)),
        "project": _session_project(session) or _role(session),
        "hr": _role(session),
        "behavioural": _role(session),
        "managerial": _role(session),
    }[category]
    difficulty = min(5, max(1, (seq + 1) // 2))
    return {
        "question_text": _question_template(session, category, seq, variant),
        "question_type": category,
        "source": "fallback_question_bank",
        "topic": topic,
        "difficulty": difficulty,
        "rubric": rubric_for_category(category),
        "expected_concepts": _expected_concepts(session, category, seq),
        "selection_reason": _selection_reason(session, category, seq),
        "model_name": "deterministic-fallback",
    }
