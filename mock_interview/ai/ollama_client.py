import json

import requests
from django.conf import settings

from mock_interview.ai.schemas import RESUME_PROFILE_SCHEMA


class LocalModelError(RuntimeError):
    pass


def _dummy_mode():
    return bool(settings.MOCK_INTERVIEW.get("DUMMY_MODE", False))


# In DUMMY_MODE every model call is served from the app's deterministic
# fallbacks by raising LocalModelError -- except resume normalization, which has
# no fallback, so it returns this canned profile instead.
_DUMMY_RESUME_PROFILE = {
    "skills": [
        "Python",
        "Django",
        "REST APIs",
        "PostgreSQL",
        "JavaScript",
        "Git",
        "Data Structures",
        "Problem Solving",
    ],
    "projects": [
        "Built a Django web application with role-based dashboards and "
        "authentication.",
        "Developed and tested a REST API backed by PostgreSQL.",
    ],
    "education": [
        "B.E. Computer Science and Engineering (pursuing)",
    ],
    "experience": [
        "Academic projects and coursework",
    ],
    "certifications": [],
}


def configured_model(purpose="interactive"):
    if purpose == "report":
        return settings.MOCK_INTERVIEW.get(
            "OLLAMA_REPORT_MODEL",
            settings.MOCK_INTERVIEW.get("OLLAMA_MODEL", "qwen3:8b"),
        )
    return settings.MOCK_INTERVIEW.get(
        "OLLAMA_INTERACTIVE_MODEL",
        settings.MOCK_INTERVIEW.get("OLLAMA_MODEL", "qwen3:4b"),
    )


def _api_url():
    base_url = settings.MOCK_INTERVIEW.get(
        "OLLAMA_BASE_URL", "http://127.0.0.1:11434"
    ).rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions", "openai"
    return f"{base_url}/api/chat", "ollama"


def chat_json(
    messages,
    schema,
    *,
    temperature=0.2,
    timeout=None,
    model=None,
    context_tokens=None,
    max_output_tokens=None,
):
    if _dummy_mode():
        if schema is RESUME_PROFILE_SCHEMA:
            return dict(_DUMMY_RESUME_PROFILE)
        raise LocalModelError(
            "DUMMY_MODE: local model disabled; using deterministic fallback."
        )

    url, api_style = _api_url()
    model = model or configured_model()
    timeout = timeout or float(settings.MOCK_INTERVIEW.get("OLLAMA_TIMEOUT", 120))

    if api_style == "ollama":
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": settings.MOCK_INTERVIEW.get("OLLAMA_THINK", "false")
            if isinstance(settings.MOCK_INTERVIEW.get("OLLAMA_THINK"), bool)
            else str(settings.MOCK_INTERVIEW.get("OLLAMA_THINK", "false")).lower()
            in {"1", "true", "yes"},
            "format": schema,
            "options": {
                "temperature": temperature,
                "num_ctx": context_tokens
                or settings.MOCK_INTERVIEW.get("OLLAMA_CONTEXT_TOKENS", 4096),
                "num_predict": max_output_tokens
                or settings.MOCK_INTERVIEW.get("OLLAMA_MAX_OUTPUT_TOKENS", 1024),
            },
        }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if api_style == "ollama":
            content = body["message"]["content"]
        else:
            content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content) if isinstance(content, str) else content
    except (requests.RequestException, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LocalModelError(f"Local model request failed: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LocalModelError("Local model did not return a JSON object.")
    return parsed


def health():
    if _dummy_mode():
        return True
    url, api_style = _api_url()
    if api_style == "ollama":
        health_url = url.replace("/api/chat", "/api/tags")
    else:
        health_url = url.replace("/chat/completions", "/models")
    try:
        response = requests.get(health_url, timeout=3)
        return response.ok
    except requests.RequestException:
        return False


def model_available(model=None):
    """Check that the configured model is advertised by the local server."""
    if _dummy_mode():
        return True
    url, api_style = _api_url()
    model = model or configured_model()
    models_url = (
        url.replace("/api/chat", "/api/tags")
        if api_style == "ollama"
        else url.replace("/chat/completions", "/models")
    )
    try:
        response = requests.get(models_url, timeout=3)
        response.raise_for_status()
        body = response.json()
        if api_style == "ollama":
            names = {
                item.get("name")
                for item in body.get("models", [])
                if isinstance(item, dict)
            }
        else:
            names = {
                item.get("id")
                for item in body.get("data", [])
                if isinstance(item, dict)
            }
        return model in names or f"{model}:latest" in names
    except (requests.RequestException, TypeError, ValueError):
        return False
