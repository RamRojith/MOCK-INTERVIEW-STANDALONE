import re
from collections import Counter, defaultdict

import networkx as nx


GRAPH_VERSION = "student_interview_graph_v1"
MAX_RESUME_ITEMS_PER_TYPE = 20
MAX_REPORT_ITEMS = 12


def build_resume_information_graph(resume):
    """Create a compact resume graph from the normalized resume profile."""
    graph = nx.DiGraph()
    profile = resume.structured_profile if isinstance(resume.structured_profile, dict) else {}
    student_id = _node_id("student", getattr(resume, "student_employee_id", "student"))
    resume_id = _node_id("resume", str(getattr(resume, "public_id", "resume")))

    _add_node(
        graph,
        student_id,
        "Student",
        "student",
        employee_id=getattr(resume, "student_employee_id", ""),
        name=getattr(resume, "student_name", ""),
    )
    _add_node(
        graph,
        resume_id,
        getattr(resume, "original_filename", "Resume"),
        "resume",
    )
    _add_edge(graph, student_id, resume_id, "uploaded")

    skills = _clean_items(profile.get("skills"), MAX_RESUME_ITEMS_PER_TYPE)
    projects = _clean_items(profile.get("projects"), MAX_RESUME_ITEMS_PER_TYPE)
    education = _clean_items(profile.get("education"), MAX_RESUME_ITEMS_PER_TYPE)
    experience = _clean_items(profile.get("experience"), MAX_RESUME_ITEMS_PER_TYPE)
    certifications = _clean_items(profile.get("certifications"), MAX_RESUME_ITEMS_PER_TYPE)

    for skill in skills:
        skill_id = _node_id("skill", skill)
        _add_node(graph, skill_id, skill, "skill")
        _add_edge(graph, resume_id, skill_id, "mentions_skill")

    for project in projects:
        project_id = _node_id("project", project)
        _add_node(graph, project_id, project, "project")
        _add_edge(graph, resume_id, project_id, "mentions_project")
        for skill in _mentioned_items(project, skills):
            _add_edge(graph, project_id, _node_id("skill", skill), "uses_skill")

    for item in education:
        node_id = _node_id("education", item)
        _add_node(graph, node_id, item, "education")
        _add_edge(graph, resume_id, node_id, "mentions_education")

    for item in experience:
        node_id = _node_id("experience", item)
        _add_node(graph, node_id, item, "experience")
        _add_edge(graph, resume_id, node_id, "mentions_experience")
        for skill in _mentioned_items(item, skills):
            _add_edge(graph, node_id, _node_id("skill", skill), "uses_skill")

    for item in certifications:
        node_id = _node_id("certification", item)
        _add_node(graph, node_id, item, "certification")
        _add_edge(graph, resume_id, node_id, "mentions_certification")

    return _export_graph(
        graph,
        "resume",
        {
            "top_skills": _central_labels(graph, "skill", limit=8),
            "project_focus": projects[:6],
            "question_focus": _resume_question_focus(projects, skills),
            "resume_coverage": {
                "skills": len(skills),
                "projects": len(projects),
                "education": len(education),
                "experience": len(experience),
                "certifications": len(certifications),
            },
        },
    )


def build_session_information_graph(session, evaluations):
    """Create an interview graph from questions, answers, scoring, and speech metrics."""
    graph = nx.DiGraph()
    session_id = _node_id("session", str(getattr(session, "public_id", getattr(session, "pk", "session"))))
    _add_node(
        graph,
        session_id,
        getattr(session, "role", "Interview session"),
        "session",
        round=getattr(session, "interview_round", ""),
        difficulty=getattr(session, "difficulty", ""),
    )

    resume_graph = {}
    if getattr(session, "resume", None):
        resume_graph = getattr(session.resume, "information_graph", {}) or {}
        resume_id = _node_id("resume", str(getattr(session.resume, "public_id", "resume")))
        _add_node(graph, resume_id, getattr(session.resume, "original_filename", "Resume"), "resume")
        _add_edge(graph, session_id, resume_id, "uses_resume")
        for skill in resume_graph.get("insights", {}).get("top_skills", [])[:8]:
            skill_id = _node_id("skill", skill)
            _add_node(graph, skill_id, skill, "skill")
            _add_edge(graph, resume_id, skill_id, "contains_skill")

    for skill in _clean_items(getattr(session, "target_skills", []), limit=10):
        skill_id = _node_id("target_skill", skill)
        _add_node(graph, skill_id, skill, "target_skill")
        _add_edge(graph, session_id, skill_id, "targets_skill")

    dimension_totals = defaultdict(list)
    improvement_counter = Counter()
    missing_counter = Counter()
    speech_observations = []

    for evaluation in evaluations:
        answer = evaluation.answer
        question = answer.question
        question_id = _node_id("question", str(question.public_id))
        answer_id = _node_id("answer", str(answer.public_id))
        evaluation_id = _node_id("evaluation", str(evaluation.pk or question.public_id))

        _add_node(
            graph,
            question_id,
            f"Q{question.sequence_number}",
            "question",
            text=question.question_text[:500],
            question_type=question.question_type,
        )
        _add_node(
            graph,
            answer_id,
            f"Answer {question.sequence_number}",
            "answer",
            word_count=(answer.speech_metrics or {}).get("word_count"),
            duration_seconds=answer.duration_seconds,
        )
        _add_node(
            graph,
            evaluation_id,
            f"Evaluation {question.sequence_number}",
            "evaluation",
            total_score=float(evaluation.total_score),
        )
        _add_edge(graph, session_id, question_id, "asked")
        _add_edge(graph, question_id, answer_id, "answered_by")
        _add_edge(graph, answer_id, evaluation_id, "evaluated_as")

        for concept in _clean_items(question.expected_concepts, limit=8):
            concept_id = _node_id("concept", concept)
            _add_node(graph, concept_id, concept, "concept")
            _add_edge(graph, question_id, concept_id, "expects_concept")

        for name, score in (evaluation.dimension_scores or {}).items():
            dimension = _clean_label(name)
            if not dimension:
                continue
            numeric_score = _safe_float(score)
            dimension_id = _node_id("dimension", dimension)
            _add_node(graph, dimension_id, dimension.replace("_", " ").title(), "dimension")
            _add_edge(graph, evaluation_id, dimension_id, "scores_dimension", score=numeric_score)
            dimension_totals[dimension].append(numeric_score)

        for item in _clean_items(evaluation.improvement_actions, limit=5):
            improvement_counter[item] += 1
            item_id = _node_id("improvement", item)
            _add_node(graph, item_id, item, "improvement")
            _add_edge(graph, evaluation_id, item_id, "recommends_improvement")

        for item in _clean_items(evaluation.missing_concepts, limit=5):
            missing_counter[item] += 1
            item_id = _node_id("missing", item)
            _add_node(graph, item_id, item, "missing_concept")
            _add_edge(graph, evaluation_id, item_id, "missed_concept")

        for observation in _speech_observations(answer.speech_metrics or {}):
            speech_observations.append(observation)
            observation_id = _node_id("speech", observation)
            _add_node(graph, observation_id, observation, "speech_observation")
            _add_edge(graph, answer_id, observation_id, "shows_speech_pattern")

    dimension_averages = {
        name: round(sum(values) / len(values), 2)
        for name, values in dimension_totals.items()
        if values
    }
    weakest = sorted(dimension_averages.items(), key=lambda item: item[1])[:3]
    strongest = sorted(
        dimension_averages.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]

    insights = {
        "central_concepts": _central_labels(graph, "skill", limit=6)
        + _central_labels(graph, "concept", limit=6),
        "strongest_dimensions": [
            {"name": name, "score": score} for name, score in strongest
        ],
        "weakest_dimensions": [
            {"name": name, "score": score} for name, score in weakest
        ],
        "priority_improvements": [
            item for item, _count in improvement_counter.most_common(MAX_REPORT_ITEMS)
        ],
        "missing_concepts": [
            item for item, _count in missing_counter.most_common(MAX_REPORT_ITEMS)
        ],
        "speech_observations": _unique(speech_observations)[:MAX_REPORT_ITEMS],
        "resume_question_focus": resume_graph.get("insights", {}).get("question_focus", [])[:6],
    }
    return _export_graph(graph, "interview_session", insights)


def graph_prompt_context(resume):
    graph = getattr(resume, "information_graph", None) or {}
    insights = graph.get("insights", {}) if isinstance(graph, dict) else {}
    return {
        "top_skills": insights.get("top_skills", [])[:8],
        "project_focus": insights.get("project_focus", [])[:6],
        "question_focus": insights.get("question_focus", [])[:6],
    }


def _export_graph(graph, graph_type, insights):
    return {
        "version": GRAPH_VERSION,
        "library": "networkx",
        "graph_type": graph_type,
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "nodes": [
            {"id": node_id, **data}
            for node_id, data in sorted(graph.nodes(data=True))
        ],
        "edges": [
            {"source": source, "target": target, **data}
            for source, target, data in sorted(
                graph.edges(data=True),
                key=lambda item: (item[0], item[1], item[2].get("relation", "")),
            )
        ],
        "insights": insights,
    }


def _add_node(graph, node_id, label, node_type, **attrs):
    graph.add_node(
        node_id,
        label=str(label or node_type)[:300],
        type=node_type,
        **{key: value for key, value in attrs.items() if value not in (None, "")},
    )


def _add_edge(graph, source, target, relation, **attrs):
    graph.add_edge(
        source,
        target,
        relation=relation,
        **{key: value for key, value in attrs.items() if value not in (None, "")},
    )


def _node_id(node_type, value):
    return f"{node_type}:{_clean_label(value)[:80] or 'unknown'}"


def _clean_label(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _clean_items(value, limit):
    if not isinstance(value, (list, tuple)):
        return []
    items = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text and text not in items:
            items.append(text[:500])
        if len(items) >= limit:
            break
    return items


def _mentioned_items(text, items):
    normalized_text = str(text).lower()
    return [
        item for item in items
        if item.lower() in normalized_text
    ][:8]


def _resume_question_focus(projects, skills):
    focus = []
    for project in projects[:6]:
        matched_skills = _mentioned_items(project, skills)
        if matched_skills:
            focus.append(
                f"Ask how the student used {', '.join(matched_skills[:3])} in {project}."
            )
        else:
            focus.append(f"Ask the student to explain the design and outcome of {project}.")
    if not focus and skills:
        focus = [f"Ask a practical question about {skill}." for skill in skills[:6]]
    return focus[:6]


def _central_labels(graph, node_type, limit):
    nodes = [node for node, data in graph.nodes(data=True) if data.get("type") == node_type]
    if not nodes:
        return []
    centrality = nx.degree_centrality(graph.to_undirected()) if graph.number_of_nodes() > 1 else {}
    ranked = sorted(
        nodes,
        key=lambda node: (centrality.get(node, 0), graph.degree(node)),
        reverse=True,
    )
    return [graph.nodes[node]["label"] for node in ranked[:limit]]


def _speech_observations(metrics):
    observations = []
    wpm = _safe_float(metrics.get("words_per_minute"))
    if wpm and wpm < 90:
        observations.append("Speaking pace may be slow; practice concise delivery.")
    elif wpm and wpm > 170:
        observations.append("Speaking pace may be fast; slow down for clarity.")

    filler_count = int(_safe_float(metrics.get("filler_count")))
    if filler_count >= 3:
        observations.append("Frequent filler words detected; practice cleaner transitions.")

    pause_seconds = _safe_float(metrics.get("pause_seconds"))
    if pause_seconds >= 5:
        observations.append("Long pauses detected; use a simple answer structure before speaking.")

    quality = metrics.get("transcript_quality") or {}
    if quality.get("status") == "needs_review":
        observations.append("Transcript quality needed review; scoring should rely on the confirmed transcript.")
    return observations


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _unique(items):
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result
