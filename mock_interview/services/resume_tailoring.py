"""Evidence-safe helpers for tailoring an uploaded resume.

Nothing in this module invents candidate details.  Skill categories are built
from the skills already present in the upload, the target role is taken from
the supplied job title or job description, and the optional rewrite guard
rejects any wording that introduces a new number, technology, or named detail.
"""

from __future__ import annotations

import re

from mock_interview.services.ats import (
    ACTION_VERBS,
    _BULLET_RE,
    _collect_bullets,
    _detect_sections,
    _lines,
    _normalize_heading,
)


# ── Skill groups ──────────────────────────────────────────────────────────
SKILL_CATEGORY_ORDER = (
    ("programming", "Programming Languages"),
    ("ai_ml", "AI / Machine Learning"),
    ("frameworks", "Frameworks and Libraries"),
    ("databases", "Databases"),
    ("cloud", "Cloud and Deployment"),
    ("data", "Data Engineering"),
    ("tools", "Developer Tools"),
    ("domain", "Relevant Domain Skills"),
)
SKILL_CATEGORY_LABELS = dict(SKILL_CATEGORY_ORDER)

_CATEGORY_SKILLS = {
    "programming": (
        "python", "java", "javascript", "typescript", "c", "c++", "c#",
        "go", "golang", "rust", "ruby", "php", "kotlin", "swift", "r",
        "matlab", "scala", "dart", "html", "css", "bash", "shell scripting",
        "powershell", "vba", "visual basic",
    ),
    "ai_ml": (
        "machine learning", "deep learning", "artificial intelligence", "ai",
        "natural language processing", "nlp", "computer vision",
        "tensorflow", "pytorch", "scikit-learn", "sklearn", "keras",
        "opencv", "hugging face", "transformers", "langchain", "llm",
        "generative ai", "data science", "reinforcement learning",
    ),
    "frameworks": (
        "django", "flask", "fastapi", "react", "react.js", "reactjs",
        "angular", "vue", "vue.js", "vuejs", "node.js", "nodejs",
        "node", "express", "express.js", "spring", "spring boot",
        ".net", "asp.net", "laravel", "bootstrap", "tailwind css",
        "jquery", "next.js", "nextjs", "flutter", "react native",
        "rest api", "rest apis", "restful api", "restful apis",
        "graphql", "microservices",
    ),
    "databases": (
        "sql", "mysql", "postgresql", "postgres", "sqlite", "mongodb",
        "oracle", "sql server", "microsoft sql server", "redis",
        "cassandra", "dynamodb", "firebase", "nosql", "pl/sql", "t-sql",
        "database design", "data modeling",
    ),
    "cloud": (
        "aws", "amazon web services", "azure", "microsoft azure", "gcp",
        "google cloud", "google cloud platform", "docker", "kubernetes",
        "k8s", "jenkins", "ci/cd", "cicd", "continuous integration",
        "continuous deployment", "github actions", "gitlab ci",
        "terraform", "ansible", "heroku", "vercel", "netlify",
        "cloud computing", "serverless", "aws lambda", "lambda",
        "amazon ec2", "ec2", "amazon s3", "s3",
    ),
    "data": (
        "data analysis", "data analytics", "data engineering",
        "data visualization", "data visualisation", "etl", "elt",
        "apache spark", "spark", "hadoop", "kafka", "apache kafka",
        "airflow", "apache airflow", "pandas", "numpy", "power bi",
        "tableau", "microsoft excel", "ms excel", "excel", "matplotlib",
        "seaborn", "data mining", "statistics", "big data",
    ),
    "tools": (
        "git", "github", "gitlab", "jira", "postman", "vs code",
        "visual studio code", "visual studio", "eclipse", "intellij",
        "pycharm", "linux", "unix", "windows", "maven", "gradle", "npm",
        "webpack", "figma", "canva", "adobe photoshop", "photoshop",
        "adobe illustrator", "illustrator", "jupyter", "jupyter notebook",
        "google colab", "colab", "notion", "slack", "trello", "confluence",
    ),
    "domain": (
        "problem solving", "communication", "leadership", "teamwork",
        "time management", "agile", "agile methodology", "scrum", "kanban",
        "project management", "data structures", "algorithms",
        "object oriented programming", "oop", "operating systems",
        "computer networks", "computer network", "dbms",
        "software engineering", "cybersecurity", "networking",
        "business analysis", "stakeholder management", "product management",
        "ui design", "ux design", "user research", "wireframing",
        "prototyping", "design systems", "seo",
        "search engine optimization", "social media marketing",
        "digital marketing", "content marketing", "email marketing",
        "google analytics",
    ),
}

_SKILL_CATEGORY_BY_KEY = {}
for _category, _names in _CATEGORY_SKILLS.items():
    for _name in _names:
        _SKILL_CATEGORY_BY_KEY[_name] = _category

_SKILL_DISPLAY = {
    "postgres": "PostgreSQL",
    "js": "JavaScript",
    "ts": "TypeScript",
    "k8s": "Kubernetes",
    "golang": "Go",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "reactjs": "React",
    "nextjs": "Next.js",
    "powerbi": "Power BI",
    "sklearn": "scikit-learn",
    "amazon web services": "AWS",
    "microsoft azure": "Azure",
    "google cloud platform": "Google Cloud Platform",
    "rest api": "REST API",
    "rest apis": "REST APIs",
    "restful api": "RESTful API",
    "restful apis": "RESTful APIs",
    "ci cd": "CI/CD",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
}

_LABEL_HINTS = {
    "programming": ("programming", "language", "languages", "coding"),
    "ai_ml": (
        "ai", "machine learning", "ml", "artificial intelligence",
        "data science",
    ),
    "frameworks": (
        "framework", "frameworks", "library", "libraries", "web",
    ),
    "databases": ("database", "databases", "db", "data store"),
    "cloud": (
        "cloud", "deployment", "devops", "infrastructure", "containers",
    ),
    "data": (
        "data", "analytics", "data engineering", "visualization",
        "visualisation", "bi",
    ),
    "tools": (
        "tool", "tools", "platform", "platforms", "software", "ide",
        "version control",
    ),
    "domain": (
        "domain", "soft skills", "other", "additional", "relevant",
        "concepts", "fundamentals",
    ),
}

_TECH_TERMS = frozenset(
    term for term in _SKILL_CATEGORY_BY_KEY
    if len(term) >= 3 or term in {"c++", "c#", "js", "ts", "ml", "ai", "nlp"}
)
_TECH_EQUIVALENTS = {
    "postgres": ("postgres", "postgresql"),
    "postgresql": ("postgres", "postgresql"),
    "js": ("js", "javascript"),
    "javascript": ("js", "javascript"),
    "ts": ("ts", "typescript"),
    "typescript": ("ts", "typescript"),
    "k8s": ("k8s", "kubernetes"),
    "kubernetes": ("k8s", "kubernetes"),
    "nodejs": ("nodejs", "node.js", "node js"),
    "node.js": ("nodejs", "node.js", "node js"),
    "reactjs": ("reactjs", "react", "react.js"),
    "react": ("reactjs", "react", "react.js"),
    "nextjs": ("nextjs", "next.js"),
    "next.js": ("nextjs", "next.js"),
    "powerbi": ("powerbi", "power bi"),
    "power bi": ("powerbi", "power bi"),
    "sklearn": ("sklearn", "scikit-learn"),
    "scikit-learn": ("sklearn", "scikit-learn"),
    "gcp": ("gcp", "google cloud", "google cloud platform"),
    "google cloud": ("gcp", "google cloud", "google cloud platform"),
    "google cloud platform": ("gcp", "google cloud", "google cloud platform"),
    "aws": ("aws", "amazon web services"),
    "amazon web services": ("aws", "amazon web services"),
    "ml": ("ml", "machine learning"),
    "machine learning": ("ml", "machine learning"),
    "nlp": ("nlp", "natural language processing"),
    "natural language processing": ("nlp", "natural language processing"),
    "dl": ("dl", "deep learning"),
    "deep learning": ("dl", "deep learning"),
    "cv": ("cv", "computer vision"),
    "computer vision": ("cv", "computer vision"),
    "rest": ("rest", "rest api", "rest apis", "restful"),
    "rest api": ("rest", "rest api", "rest apis", "restful"),
    "rest apis": ("rest", "rest api", "rest apis", "restful"),
    "ci/cd": ("ci/cd", "cicd", "ci cd", "continuous integration"),
    "cicd": ("ci/cd", "cicd", "ci cd", "continuous integration"),
    "oop": ("oop", "object oriented programming"),
    "object oriented programming": ("oop", "object oriented programming"),
}

_RATING_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—:|]\s*)?\d{1,3}\s*%\s*$"
)
_RATING_PREFIX_RE = re.compile(
    r"^\s*\d{1,3}\s*%\s*(?:[-–—:|]\s*)?"
)
_RATING_PAREN_RE = re.compile(
    r"\s*\(\s*\d{1,3}\s*%\s*\)\s*$"
)
_PROFICIENCY_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—:|]\s*)?"
    r"(?:beginner|basic|intermediate|advanced|expert|proficient)"
    r"\s*$",
    re.IGNORECASE,
)
_PROFICIENCY_PAREN_RE = re.compile(
    r"\s*\(\s*(?:beginner|basic|intermediate|advanced|expert|proficient)"
    r"\s*\)\s*$",
    re.IGNORECASE,
)
_PROGRESS_RE = re.compile(r"[▮▯■□▪▫●○◍◌⬤]{2,}")
_SKILL_SPLIT_RE = re.compile(r"\s*[,;|]\s*")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_PLACEHOLDER_RE = re.compile(r"\[add [^\]]+\]", re.IGNORECASE)
_FIRST_PERSON_RE = re.compile(
    r"\b(?:i|me|my|mine|we|our|ours)\b", re.IGNORECASE
)
_CAPITAL_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9.+#/-]*\b")

_NON_FACT_CAPS = frozenset(
    (
        "a", "an", "and", "as", "at", "but", "by", "for", "from", "he",
        "her", "his", "in", "is", "it", "its", "my", "of", "on", "or",
        "our", "she", "that", "the", "their", "them", "these", "they",
        "this", "those", "to", "was", "we", "were", "with", "you", "your",
        # Common objective/summary sentence starters.  These are not candidate
        # facts, so they must not cause an otherwise safe rewrite to fail.
        "targeting", "seeking", "aspiring", "pursuing", "currently",
        "fresher", "intern", "experienced", "professional", "candidate",
        "skilled", "motivated", "passionate", "dedicated", "hands-on",
        "strong", "technical", "final", "results-driven",
    )
)

_KNOWN_ROLES = (
    "software engineer", "software developer", "backend developer",
    "back-end developer", "frontend developer", "front-end developer",
    "full stack developer", "full-stack developer", "web developer",
    "python developer", "java developer", "javascript developer",
    "mobile developer", "android developer", "ios developer",
    "machine learning engineer", "ai engineer", "data scientist",
    "data analyst", "data engineer", "business analyst",
    "devops engineer", "cloud engineer", "qa engineer",
    "quality assurance engineer", "database administrator",
    "systems engineer", "system engineer", "network engineer",
    "cybersecurity analyst", "security analyst", "ui designer",
    "ux designer", "ui/ux designer", "product manager",
    "project manager", "technical support engineer",
    "site reliability engineer", "solutions architect",
    "research engineer", "research scientist",
)
_ROLE_DISPLAY = {
    role: " ".join(
        part.upper() if part in {"ai", "qa", "ui", "ux", "ios"}
        else part.capitalize()
        for part in role.replace("/", " / ").split()
    ).replace(" / ", "/")
    for role in _KNOWN_ROLES
}

_STRONG_STUDENT_SIGNALS = (
    "fresher", "final year", "final-year", "pursuing", "undergraduate",
    "seeking an entry", "entry-level", "recent graduate",
    "expected graduation", "currently studying", "student at",
)
_EDUCATION_SIGNALS = (
    "b.tech", "b.e.", "bachelor", "master", "bsc", "b.sc", "mca", "bca",
    "degree",
)


# ── small helpers ─────────────────────────────────────────────────────────
def _normal(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clean_skill_text(value):
    text = _PROGRESS_RE.sub("", str(value or ""))
    text = _RATING_PAREN_RE.sub("", text)
    text = _RATING_SUFFIX_RE.sub("", text)
    text = _RATING_PREFIX_RE.sub("", text)
    text = _PROFICIENCY_PAREN_RE.sub("", text)
    text = _PROFICIENCY_SUFFIX_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    return _normal(text).strip(".,;:|-–— ")


def _clean_rewrite_line(value):
    text = _BULLET_RE.sub("", _normal(value))
    return text.strip()


def _category_for_label(label):
    low = _normal(label).lower().strip(" :.-")
    if not low:
        return ""
    if low in SKILL_CATEGORY_LABELS:
        return low
    for category, hints in _LABEL_HINTS.items():
        if any(hint == low or hint in low for hint in hints):
            return category
    return ""


def _split_skill_label(line):
    if ":" not in line:
        return "", line
    label, value = line.split(":", 1)
    if _category_for_label(label):
        return label, value
    # A colon can also separate a skill from a proficiency word.
    if value.strip().lower() in {
        "beginner", "basic", "intermediate", "advanced", "expert", "proficient",
    }:
        return "", label
    return "", line


def _category_for_skill(skill):
    low = _normal(skill).lower().strip(".,;:")
    if not low:
        return "domain"
    if low in _SKILL_CATEGORY_BY_KEY:
        return _SKILL_CATEGORY_BY_KEY[low]
    best = ""
    best_len = 0
    for term, category in _SKILL_CATEGORY_BY_KEY.items():
        if len(term) < 3 and term not in {"c++", "c#"}:
            continue
        if re.search(
            rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", low
        ) and len(term) > best_len:
            best = category
            best_len = len(term)
    return best or "domain"


def _skill_display(skill):
    text = _normal(skill).strip(".,;:|-–— ")
    key = text.lower()
    return _SKILL_DISPLAY.get(key, text)


def _matches_keyword(skill, keywords):
    if not keywords:
        return False
    low = skill.lower()
    for keyword in keywords:
        kw = str(keyword).lower().strip()
        if not kw:
            continue
        if low == kw or (len(kw) >= 3 and (kw in low or low in kw)):
            return True
    return False


def group_skills(skill_lines, matched_keywords=None):
    """Return (labelled skill lines, flat skill names).

    Only skills already present in ``skill_lines`` are returned.  Ratings and
    progress bars are removed; nothing from the job description is added.
    """
    grouped = {key: [] for key, _ in SKILL_CATEGORY_ORDER}
    seen = {}
    keywords = list(matched_keywords or [])

    for raw_line in skill_lines or []:
        line = _clean_skill_text(raw_line)
        if not line:
            continue
        label, content = _split_skill_label(line)
        label_category = _category_for_label(label)
        pieces = _SKILL_SPLIT_RE.split(content)
        for piece in pieces:
            name = _clean_skill_text(piece)
            if not name:
                continue
            if ":" in name:
                # Keep phrases such as "REST API: Django" intact rather than
                # inventing a second category from an unrecognised label.
                name = name.replace(":", " -")
            display = _skill_display(name)
            key = display.lower()
            if not key or not re.search(r"[a-z0-9]", key):
                continue
            category = label_category or _category_for_skill(display)
            existing = seen.get(key)
            if existing is None:
                seen[key] = category
                grouped[category].append(display)
            elif existing == "domain" and category != "domain":
                # Promote a skill from the catch-all group to a specific one.
                grouped[existing] = [item for item in grouped[existing]
                                     if item.lower() != key]
                seen[key] = category
                grouped[category].append(display)

    result = []
    flat = []
    for category, label in SKILL_CATEGORY_ORDER:
        names = grouped[category]
        if not names:
            continue
        # Put skills that match the job description first within their group;
        # this is a stable sort, so the candidate's own order is otherwise kept.
        names = sorted(names, key=lambda item: 0 if _matches_keyword(
            item, keywords
        ) else 1)
        flat.extend(names)
        result.append(f"{label}: {', '.join(names)}")
    return result, flat


# ── target role and career level ──────────────────────────────────────────
def _role_from_supplied(value):
    role = _normal(value).strip(" .,;:-")
    if not role:
        return ""
    low = role.lower()
    if low in {
        "job description", "requirements", "responsibilities", "about us",
        "summary", "profile", "role", "position",
    }:
        return ""
    if len(role) > 120:
        role = role[:120].rsplit(" ", 1)[0]
    if len(role.split()) > 12:
        return ""
    if role.isupper():
        role = role.title()
    return role


_ROLE_WORDS = (
    "developer", "engineer", "analyst", "designer", "manager", "scientist",
    "architect", "administrator", "consultant", "specialist", "intern",
    "associate", "technician", "programmer",
)


def _role_from_text(text, *, allow_first_line=True):
    low = _normal(text).lower()
    if not low:
        return ""
    hits = []
    for role in _KNOWN_ROLES:
        if re.search(rf"(?<![a-z]){re.escape(role)}(?![a-z])", low):
            hits.append(role)
    if hits:
        best = max(hits, key=len)
        return _ROLE_DISPLAY.get(best, best.title())

    if not allow_first_line:
        return ""
    for line in _lines(text)[:10]:
        candidate = _BULLET_RE.sub("", line).strip()
        if not candidate:
            continue
        candidate = re.split(r"(?<=[.!?])\s+", candidate)[0].strip()
        if len(candidate.split()) > 8:
            continue
        low_candidate = candidate.lower()
        if low_candidate.startswith(
            ("we ", "our ", "the company", "about ", "at ", "and ", "or ")
        ):
            continue
        if not any(word in low_candidate for word in _ROLE_WORDS):
            continue
        return _role_from_supplied(candidate)
    return ""


def infer_target_role(job_description="", supplied="", resume_text=""):
    """Resolve a target title from the form, the JD, or the resume headline."""
    role = _role_from_supplied(supplied)
    if role:
        return role
    role = _role_from_text(job_description)
    if role:
        return role
    return _role_from_text(resume_text[:1200], allow_first_line=False)


def detect_career_level(resume, requested="auto"):
    text = str(getattr(resume, "extracted_text", "") or "")
    requested = (requested or "auto").strip().lower()
    if requested in {"fresher", "intern", "experienced"}:
        return requested

    lines = _lines(text)
    present, spans = _detect_sections(lines)
    lowered = text.lower()
    exp_text = " ".join(
        list(spans.get("experience", [])) + list(spans.get("internships", []))
    )
    exp_low = exp_text.lower()
    strong_student = any(signal in lowered for signal in _STRONG_STUDENT_SIGNALS)

    # A combined "Internships & Experience" heading is normalised to the
    # experience section, so inspect short heading-like lines as well.  This
    # keeps a heading-only internship resume from being called experienced.
    intern_heading = any(
        len(line.strip()) <= 60
        and re.search(
            r"\b(?:interns?|internships?|trainees?)\b",
            _normalize_heading(line),
        )
        for line in lines
    )
    has_internship = (
        "internships" in present
        or intern_heading
        or bool(re.search(r"\b(intern|internship|trainee)\b", exp_low))
    )
    if has_internship:
        return "intern"
    if strong_student:
        return "fresher"

    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", exp_text)]
    if exp_text and years and max(years) - min(years) >= 3:
        return "experienced"
    bullets = _collect_bullets(lines, spans)
    if exp_text and len(bullets) >= 5:
        return "experienced"
    if exp_text:
        return "experienced"

    if any(signal in lowered for signal in _EDUCATION_SIGNALS):
        return "fresher"
    if "education" in present or "projects" in present or "academic_projects" in present:
        return "fresher"
    return "unknown"


def _strip_first_person(text):
    text = _normal(text)
    text = re.sub(
        r"^\s*(?:i\s+am|i'm|i\s+was)\s+(?:a|an|the)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*i\s+have\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*my\s+", "", text, flags=re.IGNORECASE)
    return text[:1].upper() + text[1:] if text else text


def _article(word):
    return "an" if str(word or "").strip()[:1].lower() in "aeiou" else "a"


def _target_phrase(role):
    role = _normal(role).strip(" .,;:-")
    if not role:
        return ""
    if role.lower().endswith((" role", " position")):
        return f"Targeting the {role}."
    return f"Targeting {_article(role)} {role} role."


def _join_skills(skills, limit=4):
    chosen = [str(skill).strip() for skill in skills if str(skill).strip()][:limit]
    if not chosen:
        return ""
    if len(chosen) == 1:
        return chosen[0]
    if len(chosen) == 2:
        return f"{chosen[0]} and {chosen[1]}"
    return f"{', '.join(chosen[:-1])}, and {chosen[-1]}"


def build_objective(
    *,
    summary_lines=None,
    rewritten_summary=None,
    skills=None,
    has_projects=False,
    has_experience=False,
    target_role="",
    career_level="unknown",
):
    """Create a short evidence-based objective or professional summary."""
    base = " ".join(
        _normal(line) for line in (rewritten_summary or summary_lines or [])
        if _normal(line)
    )
    if base:
        text = _strip_first_person(base)
        sentences = re.split(r"(?<=[.!?])\s+", text)
        text = " ".join(sentences[:3]).strip()[:600]
        if text and not text.endswith((".", "!", "?")):
            text += "."
        if target_role and target_role.lower() not in text.lower():
            phrase = _target_phrase(target_role)
            if phrase:
                text = f"{text} {phrase}"[:700]
        return [text] if text else []

    skills = [str(skill).strip() for skill in (skills or []) if str(skill).strip()]
    if not skills and not target_role:
        return []

    skill_text = _join_skills(skills)
    level = (career_level or "unknown").lower()
    if level == "fresher":
        lead = (
            f"Fresher with project experience in {skill_text}"
            if has_projects and skill_text else
            f"Fresher with skills in {skill_text}" if skill_text else
            "Fresher"
        )
    elif level == "intern":
        lead = (
            f"Intern with project experience in {skill_text}"
            if has_projects and skill_text else
            f"Intern with skills in {skill_text}" if skill_text else
            "Intern"
        )
    elif level == "experienced":
        lead = (
            f"Experienced professional with skills in {skill_text}"
            if skill_text else
            "Experienced professional"
        )
    else:
        lead = (
            f"Hands-on project experience in {skill_text}"
            if has_projects and skill_text else
            f"Technical skills in {skill_text}" if skill_text else
            "Professional"
        )

    text = lead
    if target_role:
        phrase = _target_phrase(target_role)
        if phrase:
            text = f"{text}. {phrase}" if not text.endswith(".") else f"{text} {phrase}"
    if text and not text.endswith((".", "!", "?")):
        text += "."
    return [text[:700]]


# ── rewrite guard ─────────────────────────────────────────────────────────
def _term_in_text(term, text):
    term = str(term or "").lower().strip()
    if not term:
        return False
    text = str(text or "").lower()
    if term in {"c", "r", "go"}:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))
    return bool(re.search(
        rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text
    ))


def _equivalent_terms(term):
    return _TECH_EQUIVALENTS.get(term, (term,))


def _term_supported(term, text):
    return any(_term_in_text(candidate, text)
               for candidate in _equivalent_terms(term))


def _tech_terms(text):
    found = set()
    for term in _TECH_TERMS:
        if _term_in_text(term, text):
            found.add(term)
    return found


def _number_tokens(text):
    return {match.rstrip("%") for match in _NUMBER_RE.findall(str(text or ""))}


def _capital_tokens(text):
    found = set()
    for match in _CAPITAL_TOKEN_RE.findall(str(text or "")):
        token = match.strip(".-")
        low = token.lower().strip(".")
        if len(low) < 2:
            continue
        if low in _NON_FACT_CAPS or low in ACTION_VERBS:
            continue
        found.add(low)
    return found


def _distinctive_facts(lines):
    text = " ".join(str(line or "") for line in lines or [])
    return _number_tokens(text), _tech_terms(text), _capital_tokens(text)


def _line_is_safe(
    line,
    *,
    key,
    original_text,
    summary_source_text,
    target_role,
):
    if _PLACEHOLDER_RE.search(line):
        return False, "used an unresolved placeholder"
    if _FIRST_PERSON_RE.search(line):
        return False, "used first-person wording"
    if len(line) > 500:
        return False, "was longer than a resume bullet"

    scope = summary_source_text if key == "summary" else original_text
    allowed_numbers = _number_tokens(scope)
    for number in _number_tokens(line) - allowed_numbers:
        return False, f"introduced an unverified number ({number})"

    allowed_tech = _tech_terms(scope)
    for term in _tech_terms(line):
        if term in allowed_tech:
            continue
        if not _term_supported(term, scope):
            return False, f"introduced an unsupported technology ({term})"

    allowed_caps = _capital_tokens(scope)
    if key == "summary" and target_role:
        allowed_caps |= {token.lower() for token in target_role.split()}
    for token in _capital_tokens(line):
        if token in allowed_caps:
            continue
        return False, f"introduced an unsupported detail ({token})"
    return True, ""


def _preserves_original(original_lines, new_lines):
    old_numbers, old_tech, old_caps = _distinctive_facts(original_lines)
    new_text = " ".join(new_lines)
    new_numbers = _number_tokens(new_text)
    new_tech = _tech_terms(new_text)
    new_caps = _capital_tokens(new_text)
    if old_numbers - new_numbers:
        return False
    for term in old_tech:
        if not any(candidate in new_tech
                   for candidate in _equivalent_terms(term)):
            return False
    if old_caps - new_caps:
        return False
    return True


_REWRITABLE_KEYS = frozenset(
    ("summary", "experience", "projects", "education", "certifications")
)


def filter_rewrite_map(
    resume,
    section_lines,
    rewrite_map,
    *,
    target_role="",
    warnings=None,
):
    """Return only rewritten sections that pass the evidence checks.

    Summary text may draw on the whole resume.  Other sections are checked
    against their own original content so a rewriter cannot move a skill into a
    project or job where it was not claimed.  Skills are deliberately never
    rewritten: the deterministic grouping already keeps them evidence-based.
    """
    if not rewrite_map:
        return {}
    warnings = warnings if warnings is not None else []
    # Keep the original capitalization here: the named-detail check looks for
    # capitalized tokens such as "B.Tech", "SQL", and "REST".  The technology
    # and number checks are case-insensitive on their own.
    source_text = _normal(getattr(resume, "extracted_text", "") or "")
    safe = {}

    for key, new_lines in rewrite_map.items():
        if key not in _REWRITABLE_KEYS:
            continue
        original = [str(line).strip() for line in (section_lines.get(key) or [])
                    if str(line).strip()]
        if key != "summary" and not original:
            continue
        cleaned = []
        for raw in new_lines or []:
            line = _clean_rewrite_line(raw)
            if line and line not in cleaned:
                cleaned.append(line)
        if not cleaned:
            continue

        original_text = " ".join(original)
        kept = []
        for line in cleaned:
            is_safe, reason = _line_is_safe(
                line,
                key=key,
                original_text=original_text,
                summary_source_text=source_text,
                target_role=target_role,
            )
            if is_safe:
                kept.append(line)
            else:
                warnings.append(
                    f"Ignored a suggested {key} rewrite that {reason}."
                )
        if not kept:
            continue
        if key != "summary" and not _preserves_original(original, kept):
            warnings.append(
                f"Ignored the suggested {key} rewrite because it dropped "
                "verified details from the original."
            )
            continue
        safe[key] = kept

    return safe
