"""Evidence-based ATS resume analysis.

Design principles (see the product brief):

* Evaluate only what the resume actually contains. Never infer content that
  does not exist and never penalise a resume for something that cannot be
  evaluated -- that state is reported as ``not_evaluable`` and excluded from
  the score denominator instead of scored as zero.
* Every dimension returns ``{score, max, status, findings, recommendations}``
  so the final number is always traceable to detected evidence.
* Scoring is deterministic. Semantic matching is handled with a curated
  equivalence map so the checker keeps working when the local LLM is offline.
  The optional LLM (ai/ats_rewriter.py) only improves rewritten wording and
  can never change these numbers.

The two analysis modes:

* ``resume_only``     -- no job description. ``job_relevance`` and
  ``keyword_match`` are ``not_evaluable``; the score is a "Resume ATS Score"
  based on resume quality.
* ``resume_plus_jd``  -- a job description is supplied. Keyword and relevance
  dimensions are calculated and matched / missing keywords are reported.
"""

import re

RULES_VERSION = "ats_rules_v2"

# ── Shared patterns (also imported by resume_generator) ────────────────────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,5}\)[\s.-]?)?\d{3,5}[\s.-]?\d{3,5}"
)
_LINK_RE = re.compile(
    r"(?:linkedin\.com|github\.com|gitlab\.com|behance\.net|dribbble\.com|"
    r"stackoverflow\.com/|medium\.com/|dev\.to/|kaggle\.com/|"
    r"https?://|www\.)[^\s]+",
    re.IGNORECASE,
)
_LINKEDIN_RE = re.compile(r"linkedin\.com/[^\s]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:github\.com|gitlab\.com)/[^\s]+", re.IGNORECASE)
_PORTFOLIO_RE = re.compile(
    r"(?:behance\.net|dribbble\.com|kaggle\.com|"
    r"(?:https?://|www\.)[^\s]*(?:portfolio|\.me|\.dev|\.io|\.tech)[^\s]*)",
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"\b(?:address|location|city)\b\s*[:\-]|"
    r"\b[A-Z][a-z]+,\s*(?:[A-Z]{2}|[A-Z][a-z]+)\b(?:\s*\d{5,6})?",
)

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTHS_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|"
    r"november|december|present|current|till date|ongoing)\b",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•▪●◦·‣o]|\d+[.)])\s+")

# A number attached to a unit that signals real achievement / scale.
_METRIC_RE = re.compile(
    r"(?<![\w.])(?:\$|₹|€|£|usd|inr|rs\.?)\s?\d[\d,.]*\s?(?:k|m|bn|mn|cr|lakh|"
    r"lakhs|crore|million|billion)?"  # currency
    r"|(?<![\w.])\d[\d,.]*\s?(?:%|percent|pct)"  # percentages
    r"|(?<![\w.])\d[\d,.]*\s?(?:x|times)\b"  # multipliers ("3x")
    r"|(?<![\w.])\d[\d,.]*\+?\s?(?:users?|customers?|clients?|accounts?|"
    r"subscribers?|followers?|visitors?|downloads?|installs?|sign-?ups?|"
    r"leads?|orders?|transactions?|requests?|queries|records?|rows?|links?|"
    r"tickets?|students?|attendees?|participants?|members?|employees?|"
    r"people|teams?|projects?|deployments?|releases?|models?|datasets?|"
    r"hours?|days?|weeks?|months?|minutes?|seconds?|ms\b|"
    r"impressions?|clicks?|conversions?|sessions?|pageviews?)",
    re.IGNORECASE,
)
_OUTCOME_VERB_RE = re.compile(
    r"\b(reduc(?:e|ed|ing)|increas(?:e|ed|ing)|improv(?:e|ed|ing)|"
    r"sav(?:e|ed|ing)|boost(?:ed|ing)?|cut|cutting|decreas(?:e|ed|ing)|"
    r"lower(?:ed|ing)?|accelerat(?:e|ed|ing)|grew|grow(?:ing)?|doubl(?:e|ed)|"
    r"tripl(?:e|ed)|optimi[sz](?:e|ed|ing)|generat(?:e|ed|ing)|"
    r"achiev(?:e|ed|ing)|deliver(?:ed|ing)?|rank(?:ed|ing)?)\b",
    re.IGNORECASE,
)
# A self-rated skill percentage: a whole line that is essentially
# "<skill name> <separator> NN%" or "NN% <skill name>" or a progress bar.
_SKILL_PERCENT_LINE_RE = re.compile(
    r"^\s*[A-Za-z][A-Za-z0-9+#.&/ -]{0,38}?\s*[:\-–—|()]*\s*\d{1,3}\s?%\s*[)]?\s*$"
    r"|^\s*\d{1,3}\s?%\s*[:\-–—|]?\s*[A-Za-z][A-Za-z0-9+#.&/ -]{1,38}\s*$"
    r"|[▮▯■□▪▫●○◍◌⬤]{2,}"
)

ACTION_VERBS = frozenset(
    (
        "accelerated", "achieved", "administered", "analyzed", "analysed",
        "architected", "automated", "built", "collaborated", "conducted",
        "configured", "consolidated", "constructed", "coordinated", "created",
        "debugged", "delivered", "deployed", "designed", "developed",
        "devised", "directed", "drove", "engineered", "enhanced",
        "established", "executed", "expanded", "facilitated", "formulated",
        "founded", "generated", "implemented", "improved", "increased",
        "initiated", "integrated", "introduced", "launched", "led",
        "leveraged", "maintained", "managed", "mentored", "migrated",
        "modernized", "negotiated", "operated", "optimized", "optimised",
        "orchestrated", "organized", "organised", "overhauled", "oversaw",
        "pioneered", "planned", "produced", "programmed", "provisioned",
        "published", "reduced", "refactored", "researched", "resolved",
        "restructured", "revamped", "scaled", "shipped", "simplified",
        "spearheaded", "standardized", "streamlined", "strengthened",
        "supervised", "supported", "tested", "trained", "transformed",
        "translated", "upgraded", "wrote",
    )
)

_VAGUE_SKILLS = frozenset(
    (
        "ms office", "microsoft office", "ms word", "ms excel", "ms powerpoint",
        "microsoft word", "microsoft excel", "microsoft powerpoint", "word",
        "powerpoint", "outlook", "windows", "internet browsing",
        "internet", "email", "typing", "hardworking", "hard working",
        "team player", "quick learner", "fast learner", "self motivated",
        "self-motivated", "leadership", "communication", "time management",
        "problem solving", "problem-solving", "willing to learn",
        "good communication", "positive attitude", "punctual", "dedicated",
    )
)

_STOP_WORDS = frozenset(
    (
        "the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "with",
        "is", "are", "was", "were", "be", "been", "being", "have", "has",
        "had", "will", "would", "should", "could", "can", "may", "must",
        "we", "you", "they", "it", "this", "that", "these", "those", "our",
        "your", "their", "as", "at", "by", "from", "into", "about", "than",
        "through", "using", "use", "used", "role", "job", "team", "teams",
        "work", "working", "worked", "years", "year", "including", "include",
        "etc", "such", "well", "also", "one", "two", "able", "strong",
        "excellent", "good", "basic", "advanced", "knowledge", "experience",
        "responsibilities", "requirements", "required", "preferred",
        "candidate", "candidates", "ability", "skills", "skill", "plus",
        "who", "what", "when", "where", "how", "why", "not", "but", "all",
        "any", "some", "more", "most", "other", "will", "new",
        # job-description filler that is not a real keyword
        "build", "building", "built", "hiring", "hire", "looking", "join",
        "joining", "familiarity", "familiar", "responsible", "company",
        "companies", "seeking", "seek", "want", "wants", "need", "needs",
        "help", "helping", "make", "making", "get", "getting", "end", "own",
        "owning", "owned", "willing", "across", "within", "day", "days",
        "scalable", "scale", "services", "service", "solution", "solutions",
        "product", "products", "platform", "environment", "opportunity",
        "position", "years", "months", "week", "weeks", "understanding",
        "knowledge", "passion", "passionate", "great", "best", "world",
        "world-class", "fast", "fast-paced", "paced", "hands-on", "hands",
        "pipeline", "pipelines", "workflow", "workflows", "process",
        "processes", "stack", "tools", "tool", "technologies", "technology",
    )
)

_SECTION_SYNONYMS = {
    "contact": (
        "contact", "contact information", "contact info", "contact details",
        "personal contact", "get in touch",
    ),
    "summary": (
        "summary", "professional summary", "career summary", "profile",
        "professional profile", "career profile", "objective",
        "career objective", "professional objective", "about", "about me",
        "overview", "career overview", "professional snapshot",
    ),
    "experience": (
        "experience", "work experience", "professional experience",
        "employment", "employment history", "work history", "career history",
        "professional background", "relevant experience", "industry experience",
        "internships & experience", "internship & experience",
        "experience & internships", "internships and experience",
        "experience and internships",
    ),
    "internships": (
        "internship", "internships", "internship experience", "training",
        "industrial training", "in-plant training", "apprenticeship",
    ),
    "projects": (
        "projects", "project", "personal projects", "key projects",
        "notable projects", "selected projects", "project experience",
        "projects undertaken", "major projects", "project work",
    ),
    "real_projects": (
        "real-time projects", "real time projects", "real-time project",
        "real time project", "professional projects", "industry projects",
        "production projects", "applied projects",
    ),
    "academic_projects": (
        "academic projects", "academic project", "college projects",
        "college project", "university projects", "university project",
        "course projects", "course project", "coursework projects",
        "coursework project", "capstone project", "capstone projects",
        "mini projects", "mini project", "academic mini projects",
    ),
    "education": (
        "education", "education details", "educational qualifications",
        "educational qualification", "academic qualifications", "academics",
        "academic details", "academic background", "qualifications",
        "academic profile", "scholastics",
    ),
    "skills": (
        "skills", "technical skills", "core skills", "key skills", "skill set",
        "skillset", "core competencies", "competencies", "areas of expertise",
        "technical proficiencies", "technical expertise", "technologies",
        "tools and technologies", "tech stack", "proficiencies",
    ),
    "certifications": (
        "certifications", "certification", "certificates", "certificate",
        "courses", "online courses", "licenses", "licences",
        "certifications and courses", "training and certifications",
        "professional development",
    ),
    "achievements": (
        "achievements", "achievement", "accomplishments", "awards",
        "awards and honors", "awards and honours", "honors", "honours",
        "awards and achievements", "achievements and awards", "recognition",
        "accolades", "key achievements",
    ),
    "activities": (
        "activities", "extracurricular", "extra-curricular",
        "extracurricular activities", "co-curricular activities",
        "leadership", "leadership experience", "positions of responsibility",
        "volunteer experience", "volunteering", "community involvement",
        "clubs and societies",
    ),
    "coursework": (
        "relevant coursework", "coursework", "course work", "key courses",
        "relevant courses", "selected coursework",
    ),
    "publications": (
        "publications", "research", "research experience", "papers",
        "research publications",
    ),
    "languages": ("languages", "languages known", "language proficiency"),
    "interests": ("interests", "hobbies", "hobbies and interests"),
    "personal": (
        "personal details", "personal particulars", "personal information",
        "personal profile", "personal data", "bio data", "biodata",
        "declaration", "personal dossier",
    ),
    "references": ("references", "referees", "reference"),
}

_CORE_SECTIONS = ("contact", "education", "skills")
_EVIDENCE_SECTIONS = (
    "experience", "internships", "projects", "real_projects",
    "academic_projects", "achievements", "activities",
)

# Curated multi-word skills / tools worth detecting as JD keywords.
_KNOWN_PHRASES = (
    "machine learning", "deep learning", "natural language processing",
    "computer vision", "data analysis", "data science", "data engineering",
    "google analytics", "search engine optimization", "search engine marketing",
    "google ads", "google tag manager", "social media marketing",
    "content marketing", "email marketing", "power bi", "microsoft power bi",
    "rest api", "rest apis", "graphql", "spring boot", "node js", "react js",
    "next js", "vue js", "amazon web services", "google cloud", "microsoft azure",
    "ci cd", "unit testing", "test automation", "version control",
    "object oriented programming", "agile methodology", "scrum", "jira",
    "ci/cd", "ci cd",
    "figma", "adobe xd", "adobe photoshop", "adobe illustrator",
    "adobe premiere", "after effects", "ui design", "ux design",
    "user research", "wireframing", "prototyping", "design systems",
    "project management", "stakeholder management", "business analysis",
    "financial modeling", "financial modelling", "tally erp", "sap",
    "customer support", "technical support", "quality assurance",
    "continuous integration", "continuous deployment", "microservices",
    "distributed systems", "system design", "data structures", "algorithms",
    "pandas", "numpy", "scikit learn", "tensorflow", "pytorch", "power query",
)

_PHRASE_DISPLAY = {
    "ci/cd": "CI/CD",
    "ci cd": "CI/CD",
    "rest api": "REST API",
    "rest apis": "REST APIs",
    "google analytics": "Google Analytics",
    "search engine optimization": "SEO",
    "search engine marketing": "SEM",
    "natural language processing": "NLP",
    "machine learning": "Machine Learning",
    "power bi": "Power BI",
    "amazon web services": "AWS",
    "object oriented programming": "OOP",
    "ui design": "UI Design",
    "ux design": "UX Design",
}

_SYNONYMS = {
    "seo": {"search engine optimization", "search engine optimisation"},
    "search engine optimization": {"seo"},
    "search engine optimisation": {"seo"},
    "sem": {"search engine marketing", "paid search"},
    "search engine marketing": {"sem"},
    "smm": {"social media marketing"},
    "social media marketing": {"smm"},
    "ga": {"google analytics", "ga4"},
    "ga4": {"google analytics", "google analytics 4"},
    "google analytics": {"ga4", "ga"},
    "gtm": {"google tag manager"},
    "google tag manager": {"gtm"},
    "js": {"javascript"},
    "javascript": {"js", "ecmascript"},
    "ts": {"typescript"},
    "typescript": {"ts"},
    "k8s": {"kubernetes"},
    "kubernetes": {"k8s"},
    "postgres": {"postgresql"},
    "postgresql": {"postgres"},
    "aws": {"amazon web services"},
    "amazon web services": {"aws"},
    "gcp": {"google cloud", "google cloud platform"},
    "azure": {"microsoft azure"},
    "ml": {"machine learning"},
    "machine learning": {"ml"},
    "dl": {"deep learning"},
    "deep learning": {"dl"},
    "nlp": {"natural language processing"},
    "natural language processing": {"nlp"},
    "cv": {"computer vision"},
    "rest": {"rest api", "rest apis", "restful", "restful api"},
    "rest api": {"rest", "restful", "rest apis"},
    "rest apis": {"rest", "rest api", "restful"},
    "restful": {"rest", "rest api", "rest apis"},
    "restful api": {"rest", "rest api", "rest apis"},
    "ci/cd": {"cicd", "ci cd", "continuous integration", "continuous deployment"},
    "cicd": {"ci/cd", "continuous integration"},
    "react": {"react.js", "reactjs", "react js"},
    "react.js": {"react", "reactjs"},
    "reactjs": {"react", "react.js"},
    "node": {"node.js", "nodejs", "node js"},
    "node.js": {"node", "nodejs"},
    "nodejs": {"node", "node.js"},
    "oop": {"object oriented programming", "object-oriented programming"},
    "db": {"database"},
    "ui": {"user interface"},
    "ux": {"user experience"},
    "ui/ux": {"user interface", "user experience", "ui", "ux"},
    "power bi": {"powerbi", "microsoft power bi"},
    "powerbi": {"power bi"},
    "excel": {"microsoft excel", "ms excel", "spreadsheets"},
    "photoshop": {"adobe photoshop"},
    "illustrator": {"adobe illustrator"},
    "premiere": {"adobe premiere", "premiere pro"},
    "sklearn": {"scikit learn", "scikit-learn"},
    "scikit learn": {"sklearn", "scikit-learn"},
    "tf": {"tensorflow"},
    "agile": {"scrum", "agile methodology", "kanban"},
    "qa": {"quality assurance", "testing"},
}

_PERSONAL_INFO_PATTERNS = {
    "Government identification numbers": re.compile(
        r"\b(passport\s*(?:no\.?|number|#|:)|passport\s*no\b|"
        r"aadhaar|aadhar|uidai|"
        r"pan\s*(?:card|no\.?|number)|social security(?:\s*(?:no|number))?|"
        r"\bssn\b|s\.?s\.?s\.?\s*(?:no\.?|number|#)|"
        r"\btin\b\s*(?:no\.?|number|#|:)|tax\s*identification|"
        r"national\s*id(?:entity)?\s*(?:no\.?|number|card)|"
        r"voter\s*id|driver'?s?\s*licen[cs]e\s*(?:no\.?|number)?|"
        r"licen[cs]e\s*(?:no\.?|number))",
        re.IGNORECASE,
    ),
    "Date of birth": re.compile(
        r"\b(date\s*of\s*birth|d\.?o\.?b\.?|birth\s*date|born\s*on)\b",
        re.IGNORECASE,
    ),
    "Age": re.compile(
        r"\bage\b\s*[:\-]?\s*\d{1,2}\b|\b\d{1,2}\s*years?\s*old\b",
        re.IGNORECASE,
    ),
    "Gender": re.compile(
        r"\b(gender|\bsex\b)\s*[:\-]", re.IGNORECASE
    ),
    "Marital status": re.compile(
        r"\b(marital\s*status|civil\s*status)\b", re.IGNORECASE
    ),
    "Religion, caste or nationality": re.compile(
        r"\b(religion|caste|community|nationality)\s*[:\-]", re.IGNORECASE
    ),
    "Parent or spouse name": re.compile(
        r"\b(father'?s?\s*name|mother'?s?\s*name|spouse'?s?\s*name|"
        r"husband'?s?\s*name|wife'?s?\s*name|guardian'?s?\s*name)\b",
        re.IGNORECASE,
    ),
}

# ── Dimension weights (points out of 100) ─────────────────────────────────
DIMENSION_MAX = {
    "parseability": 15,
    "structure": 10,
    "job_relevance": 20,
    "keyword_match": 15,
    "experience_achievement": 15,
    "quantification": 10,
    "skills_alignment": 5,
    "contact": 5,
    "formatting": 5,
}
DIMENSION_ORDER = (
    "parseability", "structure", "job_relevance", "keyword_match",
    "experience_achievement", "quantification", "skills_alignment",
    "contact", "formatting",
)
DIMENSION_LABELS = {
    "parseability": "ATS Parseability",
    "structure": "Resume Structure",
    "job_relevance": "Job / Role Relevance",
    "keyword_match": "Keyword Match",
    "experience_achievement": "Experience & Achievement",
    "quantification": "Quantification & Impact",
    "skills_alignment": "Skills Alignment",
    "contact": "Contact & Professional Links",
    "formatting": "Formatting & Readability",
}


# ── small helpers ─────────────────────────────────────────────────────────
def _profile(profile):
    return profile if isinstance(profile, dict) else {}


def _profile_list(profile, key):
    value = _profile(profile).get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _normalize_heading(line):
    cleaned = line.strip().strip("*#_-–—•|>› ").rstrip(":").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.lower()


def _status(ratio, allow_critical=True):
    if ratio >= 0.8:
        return "good"
    if ratio >= 0.5 or not allow_critical:
        return "needs_improvement"
    return "critical"


def _dim(score, maximum, status, findings=None, recommendations=None):
    score = max(0, min(maximum, round(score)))
    return {
        "score": score,
        "max": maximum,
        "status": status,
        "findings": [f for f in (findings or []) if f],
        "recommendations": [r for r in (recommendations or []) if r],
    }


# ── detection layer ──────────────────────────────────────────────────────
def _detect_sections(lines):
    """Return (present_set, spans) where spans maps canonical -> [content lines]."""
    lookup = {}
    for canonical, names in _SECTION_SYNONYMS.items():
        for name in names:
            lookup[name] = canonical

    heading_idx = []
    for i, line in enumerate(lines):
        if len(line) > 46:
            continue
        norm = _normalize_heading(line)
        if not norm:
            continue
        canonical = lookup.get(norm)
        if canonical is None and 0 < len(norm) <= 32:
            # Uppercase / title heading that contains a known phrase.
            letters = re.sub(r"[^A-Za-z]", "", line)
            if letters and (line.upper() == line or line.istitle()):
                for name, canon in lookup.items():
                    if norm == name or norm.replace(" ", "") == name.replace(" ", ""):
                        canonical = canon
                        break
        if canonical:
            heading_idx.append((i, canonical))

    present = set()
    spans = {}
    for pos, (i, canonical) in enumerate(heading_idx):
        present.add(canonical)
        end = heading_idx[pos + 1][0] if pos + 1 < len(heading_idx) else len(lines)
        content = lines[i + 1:end]
        spans.setdefault(canonical, []).extend(content)
    return present, spans


def _looks_like_entry_header(line):
    """A company / title / date line rather than an achievement bullet."""
    if _BULLET_RE.match(line):
        return False
    has_date = bool(_YEAR_RE.search(line) or _MONTHS_RE.search(line))
    words = line.split()
    if has_date and len(words) <= 14:
        first = words[0].strip(".,;:").lower() if words else ""
        if first not in ACTION_VERBS:
            return True
    # "Role | Company" / "Role - Company" style
    if re.search(r".+\s[|–—-]\s.+", line) and len(words) <= 12 and not has_date:
        first = words[0].strip(".,;:").lower() if words else ""
        if first not in ACTION_VERBS:
            return True
    return False


def _is_descriptive(line):
    stripped = _BULLET_RE.sub("", line).strip()
    words = stripped.split()
    if len(words) < 4:
        return False
    if not re.search(r"[a-z]", stripped):
        return False
    if _looks_like_entry_header(stripped):
        return False
    return True


def _collect_bullets(lines, spans):
    """Achievement / description bullets from evidence sections + marked lines."""
    seen = set()
    bullets = []

    def add(line):
        stripped = _BULLET_RE.sub("", line).strip().rstrip(";,")
        key = stripped.lower()
        if stripped and key not in seen and _is_descriptive(line):
            seen.add(key)
            bullets.append(stripped)

    for canonical in _EVIDENCE_SECTIONS:
        for line in spans.get(canonical, []):
            add(line)
    for line in lines:
        if _BULLET_RE.match(line):
            add(line)
    return bullets


def _detect_contact(lines, text, student_name):
    name = bool(student_name and str(student_name).strip()) or (
        bool(lines) and 1 <= len(lines[0].split()) <= 5
        and not _EMAIL_RE.search(lines[0]) and lines[0][:1].isalpha()
    )
    return {
        "name": name,
        "email": bool(_EMAIL_RE.search(text)),
        "phone": bool(_PHONE_RE.search(text)),
        "location": bool(_LOCATION_RE.search(text)),
        "linkedin": bool(_LINKEDIN_RE.search(text)),
        "github": bool(_GITHUB_RE.search(text)),
        "portfolio": bool(_PORTFOLIO_RE.search(text)),
    }


def _detect_personal_info(text):
    found = []
    for label, pattern in _PERSONAL_INFO_PATTERNS.items():
        if pattern.search(text):
            found.append(label)
    # A labelled residential address line with a street number and a PIN/ZIP.
    if re.search(
        r"\b(residential address|permanent address|present address|"
        r"home address|address)\b\s*[:\-][^\n]*\d{1,4}[^\n]*\b\d{5,6}\b",
        text,
        re.IGNORECASE,
    ):
        found.append("Full residential address")
    return found


def _detect_skill_percent(spans, lines):
    """True when skills are shown as self-rated percentages / progress bars."""
    candidates = spans.get("skills") or []
    scope = candidates if candidates else lines
    hits = 0
    for line in scope:
        if _SKILL_PERCENT_LINE_RE.search(line.strip()):
            hits += 1
    return hits >= 1 if candidates else hits >= 2


def _profile_type(present, spans, text, bullets):
    lowered = text.lower()
    student_signals = any(
        token in lowered
        for token in (
            "fresher", "final year", "final-year", "pursuing", "undergraduate",
            "seeking an entry", "entry-level", "recent graduate",
            "expected graduation", "currently studying", "student at",
        )
    )
    if "experience" in present and bullets:
        # Multi-year date ranges *inside the experience block* suggest a real
        # professional history (education dates must not leak in here).
        exp_text = " ".join(
            spans.get("experience", []) + spans.get("internships", [])
        )
        years = [int(y) for y in _YEAR_RE.findall(exp_text)]
        if years and (max(years) - min(years)) >= 3:
            return "experienced"
        if len(bullets) >= 5:
            return "experienced"
        return "mixed"
    if student_signals or "education" in present:
        return "fresher"
    return "unknown"


# ── keyword / JD handling ────────────────────────────────────────────────
def extract_jd_keywords(job_description):
    """Extract a bounded, de-duplicated set of meaningful JD terms.

    Returns a list of ``{term, display, weight}`` dicts. ``term`` is lower-cased
    for matching; ``display`` keeps a readable casing. ``weight`` is 2 for terms
    in a requirement context or occurring more than once, else 1.
    """
    if not job_description:
        return []
    lowered = job_description.lower()
    weights = {}
    display = {}

    def bump(term, weight, shown=None):
        term = term.strip().lower()
        if not term or term in _STOP_WORDS or len(term) < 2:
            return
        weights[term] = max(weights.get(term, 0), weight)
        if shown and term not in display:
            display[term] = shown.strip()

    # Multi-word known phrases.
    for phrase in _KNOWN_PHRASES:
        if phrase in lowered:
            hard = bool(re.search(
                r"(require|must|essential|proficien|expert|strong)[^.\n]{0,40}"
                + re.escape(phrase),
                lowered,
            )) or lowered.count(phrase) > 1
            bump(phrase, 2 if hard else 1, _PHRASE_DISPLAY.get(phrase, phrase.title()))

    # Single tokens (keep tech punctuation like c++, node.js).
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{1,28}", job_description)
    counts = {}
    first_seen = {}
    for token in tokens:
        low = token.lower().strip(".-")
        if len(low) < 3 or low in _STOP_WORDS:
            continue
        counts[low] = counts.get(low, 0) + 1
        first_seen.setdefault(low, token.strip(".-"))
    req_region = ""
    match = re.search(
        r"(requirements?|qualifications?|must have|what you.ll need|"
        r"we.re looking for)(.*)",
        lowered,
        re.DOTALL,
    )
    if match:
        req_region = match.group(2)[:2000]
    for low, count in counts.items():
        weight = 2 if (count > 1 or low in req_region) else 1
        bump(low, weight, first_seen.get(low, low))

    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"term": term, "display": display.get(term, term), "weight": weight}
        for term, weight in ordered[:40]
    ]


def _haystack(resume):
    text = (getattr(resume, "extracted_text", "") or "").lower()
    profile = _profile(getattr(resume, "structured_profile", {}))
    parts = [text]
    for key in ("skills", "projects", "experience", "education", "certifications", "summary"):
        parts.extend(item.lower() for item in _profile_list(profile, key))
    return "\n".join(parts)


def _term_present(term, haystack):
    term = term.lower()
    if term in haystack:
        return True
    for syn in _SYNONYMS.get(term, ()):  # curated semantic equivalents
        if syn in haystack:
            return True
    return False


def _dedupe_overlapping(terms):
    """Drop single words that are already covered by a multi-word term."""
    phrases = [t for t in terms if " " in t]
    out = []
    for t in terms:
        low = t.lower()
        if " " not in t and any(
            re.search(rf"\b{re.escape(low)}\b", p.lower()) for p in phrases
        ):
            continue
        out.append(t)
    return out


def _match_keywords(resume, weighted_keywords):
    haystack = _haystack(resume)
    matched, missing = [], []
    matched_weight = total_weight = 0
    for entry in weighted_keywords:
        term, weight = entry["term"], entry["weight"]
        shown = entry.get("display", term)
        total_weight += weight
        if _term_present(term, haystack):
            matched.append(shown)
            matched_weight += weight
        else:
            missing.append(shown)
    matched = _dedupe_overlapping(matched)
    missing = _dedupe_overlapping(missing)
    coverage = (matched_weight / total_weight) if total_weight else 0.0
    return matched, missing, coverage


def _keyword_stuffing(resume, matched):
    text = (getattr(resume, "extracted_text", "") or "").lower()
    for term in matched:
        low = str(term).lower()
        if low and text.count(low) >= 9:
            return term
    for line in _lines(text):
        if line.count(",") >= 25:
            return "comma-separated list"
    return None


# ── dimension scorers ────────────────────────────────────────────────────
def _score_parseability(text, lines):
    findings, recs = [], []
    score = 15.0
    words = re.findall(r"\S+", text)
    total = len(words)
    if total < 25:
        score -= 8
        findings.append(
            "Almost no machine-readable text was extracted -- the file may be "
            "image-based, scanned, or built entirely from text boxes."
        )
        recs.append(
            "Export the resume as a text-based PDF from Word or Google Docs "
            "(avoid 'print to image' or scanned copies)."
        )
    replacement = text.count("�") + text.count("\x00")
    if replacement > 3:
        score -= 3
        findings.append("Unusual or corrupted characters were found in the text.")
    glued = sum(1 for w in words if len(w) > 28)
    if total and glued / total > 0.03:
        score -= 3
        findings.append(
            "Words appear joined together, which usually means a multi-column "
            "or table layout that ATS parsers read out of order."
        )
        recs.append(
            "Use a single-column layout; avoid tables and text boxes for "
            "content the ATS must read."
        )
    if "\t" in text or re.search(r"\S {3,}\S.* {3,}\S", text):
        score -= 2
        findings.append(
            "Tab stops or column gaps detected -- verify the reading order is "
            "left-to-right, top-to-bottom."
        )
    if total and not findings:
        findings.append("Text extracts cleanly with a readable reading order.")
    ratio = max(0.0, score) / 15
    return _dim(score, 15, _status(ratio), findings, recs)


def _score_structure(present, profile_type, has_summary, personal_info):
    findings, recs = [], []
    score = 0
    core_hits = [s for s in _CORE_SECTIONS if s in present]
    score += 2 * len(core_hits)  # up to 6
    for missing in (s for s in _CORE_SECTIONS if s not in present):
        findings.append(f"No clearly labelled {missing.capitalize()} section detected.")
        recs.append(
            f"Add a standard '{missing.capitalize()}' heading so ATS parsers "
            "can categorise that content."
        )
    has_evidence_section = any(s in present for s in _EVIDENCE_SECTIONS)
    if has_evidence_section:
        score += 3
    else:
        if profile_type in ("fresher", "unknown"):
            findings.append(
                "No Experience, Projects or Internship section detected. For a "
                "student / fresher profile, adding 2-3 relevant projects or an "
                "internship gives concrete evidence of practical skills."
            )
            recs.append(
                "Add a Projects section with 2-3 academic or personal projects "
                "(what you built, the tools used, and the result)."
            )
        else:
            findings.append(
                "No Experience section detected for what looks like an "
                "experienced profile."
            )
            recs.append("Add a Work Experience section with role, dates and achievements.")
    if has_summary:
        score += 1
    else:
        recs.append(
            "Add a 2-3 line professional summary at the top stating your target "
            "role and strongest skills."
        )
    if "personal" in present or personal_info:
        score -= min(2, 1 + len(personal_info) // 3)
        findings.append(
            "Space is used on a personal-details / declaration block instead of "
            "role-relevant content."
        )
        recs.append(
            "Replace the personal-particulars block with a Projects or "
            "Achievements section."
        )
    if not findings:
        findings.append("All standard sections for this profile are present and labelled.")
    score = max(0, score)
    ratio = score / 10
    return _dim(score, 10, _status(ratio), findings, recs)


def _score_job_relevance(coverage, title_overlap, evidence_factor, jd_given):
    if not jd_given:
        return _dim(
            0, 20, "not_evaluable",
            ["No target job description provided, so job-specific relevance "
             "was not calculated. The overall score reflects resume quality."],
            ["Paste the target job description to see how well the resume "
             "matches a specific role."],
        )
    raw = 0.62 * coverage + 0.15 * title_overlap + 0.23 * evidence_factor
    score = raw * 20
    ratio = score / 20
    findings, recs = [], []
    if coverage < 0.4:
        findings.append(
            "The resume covers less than half of the important terms from the "
            "job description."
        )
    if title_overlap < 0.2:
        findings.append(
            "The resume does not clearly echo the target role title or its core "
            "responsibilities."
        )
        recs.append(
            "Mirror the role title and 2-3 key responsibilities from the job "
            "description in your summary -- only where the resume already "
            "supports them."
        )
    if evidence_factor < 0.5:
        findings.append(
            "There is limited experience or project evidence backing the "
            "job-required capabilities."
        )
    if not findings:
        if ratio >= 0.8:
            findings.append("The resume aligns well with the target role.")
        else:
            findings.append(
                "Partial alignment with the target role -- broaden how clearly "
                "the resume reflects its key requirements."
            )
    return _dim(score, 20, _status(ratio), findings, recs)


def _score_keyword_match(matched, missing, coverage, stuffing, jd_given):
    if not jd_given:
        return _dim(
            0, 15, "not_evaluable",
            ["No target job description provided, so keyword matching was not "
             "performed."],
            [],
        )
    score = coverage * 15
    findings, recs = [], []
    if missing:
        findings.append(
            f"{len(missing)} of {len(matched) + len(missing)} important job "
            f"keywords are not evidenced in the resume."
        )
        recs.append(
            "Where you genuinely have the experience, add the job's exact "
            f"terminology (for example: {', '.join(missing[:6])})."
        )
    if stuffing:
        score = min(score, 11)
        findings.append(
            "Possible keyword stuffing detected -- repeating terms without "
            "context does not help and can be flagged by recruiters."
        )
        recs.append("Remove repeated keyword lists; keep each skill in context.")
    if not missing and not stuffing:
        findings.append("Strong keyword coverage against the job description.")
    elif not findings:
        findings.append("Moderate keyword coverage against the job description.")
    return _dim(score, 15, _status(score / 15), findings, recs)


def _score_experience(bullets, present, profile_type):
    findings, recs = [], []
    has_evidence_section = any(s in present for s in _EVIDENCE_SECTIONS)
    if not bullets:
        if has_evidence_section:
            findings.append(
                "An experience/projects section exists but contains no "
                "achievement-oriented bullet points describing what was done."
            )
        else:
            findings.append("No professional experience or achievement bullets detected.")
        recs.append(
            "Add 2-4 achievement bullets describing what you built, improved, "
            "managed or delivered -- start each with a strong action verb."
        )
        status = "critical"
        return _dim(2, 15, status, findings, recs)

    verb_hits = impact_hits = tech_hits = specific_hits = 0
    weak_examples = []
    for bullet in bullets:
        words = bullet.split()
        first = words[0].strip(".,;:()").lower() if words else ""
        has_verb = first in ACTION_VERBS
        has_impact = bool(
            _OUTCOME_VERB_RE.search(bullet) or _METRIC_RE.search(bullet)
        )
        has_tech = bool(
            re.search(
                r"\b(using|with|in|via|built on|leverag\w+)\b\s+[A-Z0-9]",
                bullet,
            )
            or re.search(r"\b[A-Z][A-Za-z0-9.+#]{1,}\b", bullet[3:])
        )
        is_specific = len(words) >= 8
        verb_hits += has_verb
        impact_hits += has_impact
        tech_hits += has_tech
        specific_hits += is_specific
        if not has_verb and len(weak_examples) < 3:
            weak_examples.append(bullet[:90])

    n = len(bullets)
    quality = (
        0.35 * (verb_hits / n)
        + 0.30 * (impact_hits / n)
        + 0.20 * (tech_hits / n)
        + 0.15 * (specific_hits / n)
    )
    volume = min(1.0, n / 4)
    score = volume * 3 + quality * 12

    if verb_hits / n < 0.6:
        findings.append(
            f"{n - verb_hits} of {n} bullets do not begin with a strong action "
            "verb."
        )
        recs.append(
            "Rewrite bullets to start with verbs such as Built, Developed, "
            "Led, Optimised, Reduced."
        )
    if impact_hits / n < 0.4:
        findings.append("Most bullets describe tasks rather than outcomes or results.")
        recs.append(
            "Add the result of each task (what changed, for whom, by how much)."
        )
    if not findings:
        findings.append("Bullets are action-oriented and outcome-focused.")
    return _dim(score, 15, _status(score / 15), findings, recs)


def _score_quantification(bullets, skill_percent_present):
    findings, recs = [], []
    if not bullets:
        note = (
            "Quantification could not be evaluated because no experience or "
            "project bullets were detected."
        )
        if skill_percent_present:
            note += (
                " Skill percentages are self-assessed ratings and are not "
                "counted as achievement metrics."
            )
        return _dim(0, 10, "not_evaluable", [note], [
            "Once you add experience/project bullets, include measurable "
            "results (%, time saved, users, revenue, scale)."
        ])

    metric_bullets = [b for b in bullets if _METRIC_RE.search(b)]
    outcome_metric = [
        b for b in metric_bullets if _OUTCOME_VERB_RE.search(b)
    ]
    count = len(metric_bullets)
    ratio = count / len(bullets)
    if count == 0:
        score = 2
        findings.append(
            "Limited measurable impact detected. Graduation years, phone "
            "numbers and self-rated skill percentages are not counted as "
            "achievement metrics."
        )
        recs.append(
            "Quantify at least 2-3 bullets (for example: 'cut page load time "
            "by 28%' or 'handled 50+ support tickets weekly')."
        )
    elif count >= 3 or (count >= 2 and ratio >= 0.5):
        score = 7 + (1 if outcome_metric else 0) + (1 if len(outcome_metric) >= 2 else 0)
        score = min(10, score + (1 if count >= 4 else 0))
        findings.append(f"{count} bullets contain meaningful, measurable results.")
    else:
        score = 5 + len(outcome_metric)
        findings.append(
            f"Only {count} of {len(bullets)} bullets contain a measurable "
            "result -- add a few more for a stronger impact story."
        )
    if skill_percent_present:
        recs.append(
            "Replace skill percentage ratings with a plain categorised skills "
            "list."
        )
    return _dim(score, 10, _status(score / 10, allow_critical=False), findings, recs)


def _score_skills(profile, present, text, jd_coverage, jd_given, skill_percent_present):
    findings, recs = [], []
    skills = _profile_list(profile, "skills")
    lowered_text = text.lower()
    count = len(skills)
    vague = [s for s in skills if s.lower() in _VAGUE_SKILLS]
    supported = [
        s for s in skills
        if s.lower() in lowered_text
        and lowered_text.count(s.lower()) >= 2
    ]

    if count == 0:
        findings.append("No skills were detected in a dedicated, parseable list.")
        recs.append(
            "Add a Skills section with specific tools, languages and frameworks "
            "grouped by category."
        )
        return _dim(1, 5, "critical", findings, recs)

    specific = [s for s in skills if s.lower() not in _VAGUE_SKILLS]
    score = 2.0
    if jd_given:
        score = 1 + jd_coverage * 4
        if jd_coverage < 0.5:
            findings.append(
                "Several job-required skills are missing from the skills list."
            )
    else:
        if len(specific) >= 6:
            score += 1.5
        elif len(specific) >= 3:
            score += 1.0
    if len(supported) >= min(3, count):
        score += 0.5
    else:
        findings.append(
            "Some listed skills do not appear anywhere else in the resume -- "
            "supporting evidence is limited."
        )
        recs.append(
            "Show 1-2 skills in action inside a project or experience bullet."
        )
    if vague:
        score -= min(1.0, 0.4 * len(set(v.lower() for v in vague)))
        findings.append(
            "Generic entries detected (e.g. "
            + ", ".join(sorted({v for v in vague}))[:80]
            + "). Prefer specific, verifiable skills."
        )
    if skill_percent_present:
        score -= 1.0
        findings.append(
            "Skills are shown with self-rated percentages; recruiters and ATS "
            "systems treat these as unreliable."
        )
        recs.append(
            "Replace skill percentage ratings with a plain categorised list "
            "(e.g. Design: Photoshop, Illustrator, Figma)."
        )
    if not findings:
        findings.append("Skills are specific and supported by the rest of the resume.")
    return _dim(score, 5, _status(max(0, score) / 5, allow_critical=False), findings, recs)


def _score_contact(flags, profile_type):
    findings, recs = [], []
    score = 0
    score += 1.4 if flags["email"] else 0
    score += 1.2 if flags["phone"] else 0
    score += 0.6 if flags["name"] else 0
    score += 0.4 if flags["location"] else 0
    has_link = flags["linkedin"] or flags["github"] or flags["portfolio"]
    score += 1.4 if has_link else 0

    if not flags["email"]:
        findings.append("No email address detected.")
        recs.append("Add a professional email address to the header.")
    if not flags["phone"]:
        findings.append("No phone number detected.")
    if not has_link:
        findings.append("No professional profile or portfolio link detected.")
        recs.append(
            "Add a LinkedIn URL; for technical or design roles also add a "
            "GitHub or portfolio link that shows real work."
        )
    elif not flags["linkedin"]:
        recs.append("Consider adding a LinkedIn URL alongside your other links.")
    if not findings:
        findings.append("Contact block is complete and machine-readable.")
    # Missing both email and phone is a genuine ATS blocker.
    allow_critical = not (flags["email"] or flags["phone"])
    return _dim(score, 5, _status(score / 5, allow_critical=allow_critical), findings, recs)


def _score_formatting(text, lines, bullets, parse_dim):
    findings, recs = [], []
    score = 5.0
    words = re.findall(r"\S+", text)
    total = len(words)
    if total and total < 180:
        score -= 1
        findings.append("The resume is very short and may look thin to recruiters.")
        recs.append("Aim for roughly 350-800 words of substantive content.")
    elif total > 1200:
        score -= 1.5
        findings.append("The resume is long; tighten it toward one to two pages.")
        recs.append("Trim older or low-impact content to keep it concise.")
    bullet_lines = [l for l in lines if _BULLET_RE.match(l)]
    long_paras = [l for l in lines if len(l.split()) > 45]
    if bullets and not bullet_lines:
        score -= 1
        findings.append(
            "Achievements are written as paragraphs rather than bullet points."
        )
        recs.append("Convert experience/project descriptions into short bullets.")
    if long_paras:
        score -= 1
        findings.append("Some very long paragraphs create visual clutter.")
    if parse_dim["score"] < parse_dim["max"] * 0.7:
        score -= 1
        findings.append(
            "Layout complexity is also affecting machine readability (see ATS "
            "Parseability)."
        )
    if not findings:
        findings.append("Formatting is consistent and ATS-safe.")
    return _dim(score, 5, _status(max(0, score) / 5, allow_critical=False), findings, recs)


def _action_verb_analysis(bullets):
    if not bullets:
        return {
            "status": "not_evaluable",
            "message": (
                "No experience or project bullets were detected, so "
                "action-verb usage could not be evaluated."
            ),
            "recommendation": (
                "Add achievement-oriented bullets that begin with strong "
                "action verbs (Built, Developed, Led, Optimised)."
            ),
            "strong_ratio": None,
        }
    strong = 0
    weak = []
    for bullet in bullets:
        words = bullet.split()
        first = words[0].strip(".,;:()").lower() if words else ""
        if first in ACTION_VERBS:
            strong += 1
        elif len(weak) < 3:
            weak.append(bullet[:90])
    ratio = strong / len(bullets)
    if ratio >= 0.8:
        status, message = "good", "Most bullets start with a strong action verb."
    elif ratio >= 0.5:
        status = "needs_improvement"
        message = "About half of the bullets start with a weak or passive opener."
    else:
        status = "critical"
        message = "Most bullets do not begin with a strong action verb."
    return {
        "status": status,
        "message": message,
        "recommendation": (
            "Start each bullet with a past-tense action verb and lead with the "
            "result where you have one."
        ),
        "strong_ratio": round(ratio, 2),
        "weak_examples": weak,
    }


# ── issues + recommendations ─────────────────────────────────────────────
_SEVERITY_BY_STATUS = {
    "critical": "critical",
    "needs_improvement": "warning",
    "good": "good",
    "not_evaluable": "suggestion",
}
_CRITICAL_DIMS = {"parseability", "experience_achievement", "structure", "contact"}
_POSITIVE_FINDING_RE = re.compile(
    r"^(all standard|the resume aligns well|strong keyword|moderate keyword|"
    r"bullets are action|skills are specific|contact block is complete|"
    r"formatting is consistent|text extracts cleanly|"
    r"\d+ bullets contain meaningful)",
    re.IGNORECASE,
)


def _build_issues(dimensions, personal_info):
    issues = []
    for key in DIMENSION_ORDER:
        dim = dimensions[key]
        if dim["status"] == "not_evaluable" or dim["status"] == "good":
            continue
        severity = _SEVERITY_BY_STATUS[dim["status"]]
        if severity == "critical" and key not in _CRITICAL_DIMS:
            severity = "warning"
        for finding in dim["findings"][:2]:
            if _POSITIVE_FINDING_RE.match(finding.strip()):
                continue
            issues.append({
                "category": key,
                "severity": severity,
                "message": finding,
            })
    if personal_info:
        issues.append({
            "category": "personal_info",
            "severity": "warning",
            "message": (
                "Unnecessary personal information detected ("
                + ", ".join(personal_info)
                + "). Remove government identification numbers and other "
                "details not relevant to the target role."
            ),
        })
    order = {"critical": 0, "warning": 1, "suggestion": 2}
    issues.sort(key=lambda i: order.get(i["severity"], 3))
    return issues[:12]


def _build_recommendations(dimensions, personal_info):
    ranked = []
    for key in DIMENSION_ORDER:
        dim = dimensions[key]
        priority = {"critical": 0, "needs_improvement": 1}.get(dim["status"], 3)
        for rec in dim["recommendations"]:
            ranked.append((priority, rec))
    if personal_info:
        ranked.insert(0, (
            0,
            "Remove government identification numbers, date of birth, gender, "
            "marital status and other personal details that are not relevant "
            "to the target role.",
        ))
    ranked.sort(key=lambda item: item[0])
    seen, out = set(), []
    for _, rec in ranked:
        if rec not in seen:
            seen.add(rec)
            out.append(rec)
    return out[:6]


def _score_label(score):
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Strong"
    if score >= 50:
        return "Needs Improvement"
    return "Weak"


def _explain(dimensions, mode):
    positives, negatives = [], []
    for key in DIMENSION_ORDER:
        dim = dimensions[key]
        label = DIMENSION_LABELS[key]
        if dim["status"] == "good":
            positives.append(f"{label}: solid")
        elif dim["status"] == "critical":
            negatives.append(f"{label}: needs significant work")
        elif dim["status"] == "needs_improvement":
            negatives.append(f"{label}: room to improve")
    lines = []
    if mode == "resume_only":
        lines.append(
            "No job description was supplied, so this is a Resume ATS Score "
            "based on resume quality, not job match."
        )
    lines.extend(positives[:3])
    lines.extend(negatives[:5])
    return lines


# ── public API ───────────────────────────────────────────────────────────
def analyze_resume(resume, job_description=""):
    """Return an evidence-based ATS analysis dict.

    Keys: ``score`` (0-100), ``score_label``, ``analysis_mode``,
    ``dimensions`` (rich per-dimension objects), ``dimension_scores`` (same,
    kept for backwards compatibility), ``issues``, ``recommendations``,
    ``matched_keywords``, ``missing_keywords``, ``score_explanation``,
    ``personal_info_flags``, ``action_verb_analysis``, ``rules_version``.
    """
    text = str(getattr(resume, "extracted_text", "") or "")
    profile = _profile(getattr(resume, "structured_profile", {}))
    lines = _lines(text)

    present, spans = _detect_sections(lines)
    # profile-provided sections also count as present evidence
    for key in ("skills", "projects", "experience", "education", "certifications"):
        if _profile_list(profile, key):
            present.add({"skills": "skills", "projects": "projects",
                         "experience": "experience", "education": "education",
                         "certifications": "certifications"}[key])
    if _EMAIL_RE.search(text) or _PHONE_RE.search(text):
        present.add("contact")

    bullets = _collect_bullets(lines, spans)
    if not bullets:
        for key in ("experience", "projects"):
            bullets.extend(
                b for b in _profile_list(profile, key) if _is_descriptive(b)
            )
        seen = set()
        bullets = [b for b in bullets if not (b.lower() in seen or seen.add(b.lower()))]

    contact_flags = _detect_contact(lines, text, getattr(resume, "student_name", ""))
    personal_info = _detect_personal_info(text)
    skill_percent_present = _detect_skill_percent(spans, lines)
    has_summary = "summary" in present or bool(_profile_list(profile, "summary"))
    profile_type = _profile_type(present, spans, text, bullets)

    weighted_keywords = extract_jd_keywords(job_description)
    jd_given = bool(weighted_keywords)
    mode = "resume_plus_jd" if jd_given else "resume_only"

    matched, missing, coverage = (
        _match_keywords(resume, weighted_keywords) if jd_given else ([], [], 0.0)
    )
    stuffing = _keyword_stuffing(resume, matched) if jd_given else None

    # title / responsibility overlap
    title_overlap = 0.0
    if jd_given:
        head = " ".join((job_description or "").lower().splitlines()[:6])
        jd_head_tokens = {
            t for t in re.findall(r"[a-z][a-z0-9+#.]{2,}", head)
            if t not in _STOP_WORDS
        }
        resume_head = " ".join(lines[:8]).lower() + " " + " ".join(present)
        if jd_head_tokens:
            hit = sum(1 for t in jd_head_tokens if t in resume_head)
            title_overlap = min(1.0, hit / max(4, len(jd_head_tokens)))
    evidence_factor = (
        1.0 if (any(s in present for s in _EVIDENCE_SECTIONS) and bullets)
        else 0.5 if (present & {"projects", "skills"})
        else 0.2
    )

    dimensions = {}
    dimensions["parseability"] = _score_parseability(text, lines)
    dimensions["structure"] = _score_structure(
        present, profile_type, has_summary, personal_info
    )
    dimensions["job_relevance"] = _score_job_relevance(
        coverage, title_overlap, evidence_factor, jd_given
    )
    dimensions["keyword_match"] = _score_keyword_match(
        matched, missing, coverage, stuffing, jd_given
    )
    dimensions["experience_achievement"] = _score_experience(
        bullets, present, profile_type
    )
    dimensions["quantification"] = _score_quantification(
        bullets, skill_percent_present
    )
    dimensions["skills_alignment"] = _score_skills(
        profile, present, text, coverage, jd_given, skill_percent_present
    )
    dimensions["contact"] = _score_contact(contact_flags, profile_type)
    dimensions["formatting"] = _score_formatting(
        text, lines, bullets, dimensions["parseability"]
    )

    # Order the dict and attach a human-readable label for the UI.
    dimensions = {
        key: {"label": DIMENSION_LABELS[key], **dimensions[key]}
        for key in DIMENSION_ORDER
    }

    earned = sum(
        d["score"] for k, d in dimensions.items() if d["status"] != "not_evaluable"
    )
    available = sum(
        d["max"] for k, d in dimensions.items() if d["status"] != "not_evaluable"
    )
    score = round(earned / available * 100, 2) if available else 0.0
    score = max(0.0, min(100.0, score))

    action_verbs = _action_verb_analysis(bullets)
    issues = _build_issues(dimensions, personal_info)
    recommendations = _build_recommendations(dimensions, personal_info)

    return {
        "score": score,
        "score_label": _score_label(score),
        "analysis_mode": mode,
        "jd_given": jd_given,
        "profile_type": profile_type,
        "dimensions": dimensions,
        "dimension_scores": dimensions,  # backwards-compatible alias
        "issues": issues,
        "recommendations": recommendations,
        "matched_keywords": matched,
        "missing_keywords": missing,
        "score_explanation": _explain(dimensions, mode),
        "personal_info_flags": personal_info,
        "action_verb_analysis": action_verbs,
        "rules_version": RULES_VERSION,
    }
