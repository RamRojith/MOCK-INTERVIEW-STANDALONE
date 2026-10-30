import json
import logging
import time
from contextvars import ContextVar

import requests
from django.conf import settings

from mock_interview.ai.schemas import RESUME_PROFILE_SCHEMA

logger = logging.getLogger(__name__)

# Request-scoped metadata for concurrency safety
_request_model_meta: ContextVar = ContextVar("ollama_request_model_meta", default=None)


class LocalModelError(RuntimeError):
    pass


def _dummy_mode():
    return bool(settings.MOCK_INTERVIEW.get("DUMMY_MODE", False))


def last_used_model():
    meta = _request_model_meta.get()
    if isinstance(meta, dict):
        return meta.get("successful_model")
    return None


def get_last_request_meta():
    meta = _request_model_meta.get()
    if isinstance(meta, dict):
        return dict(meta)
    return {}


def _set_request_meta(meta):
    try:
        _request_model_meta.set(meta)
    except Exception:
        pass


def _parse_route_list(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    result = []
    for item in items:
        name = str(item).strip()
        if name and name not in result:
            result.append(name)
    return result


_ROLE_ROUTE_MAP = {
    "report": "LLM_ROUTE_REPORT",
    "question": "LLM_ROUTE_QUESTION",
    "questions": "LLM_ROUTE_QUESTION",
    "interactive": "LLM_ROUTE_INTERACTIVE",
    "evaluation": "LLM_ROUTE_EVALUATION",
    "eval": "LLM_ROUTE_EVALUATION",
    "evaluate": "LLM_ROUTE_EVALUATION",
}


def _get_route_candidates(role=None, primary_model=None):
    cfg = getattr(settings, "MOCK_INTERVIEW", {}) or {}
    role = (role or "").strip().lower()
    primary_model = primary_model or configured_model(role or "interactive")
    candidates = []
    if primary_model:
        candidates.append(primary_model)
    route_key = _ROLE_ROUTE_MAP.get(role)
    if route_key and route_key in cfg:
        for m in _parse_route_list(cfg.get(route_key)):
            if m and m not in candidates:
                candidates.append(m)
    if len(candidates) == 1:
        for m in _parse_route_list(cfg.get("LLM_ROUTE_DEFAULT")):
            if m and m not in candidates:
                candidates.append(m)
    return candidates


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


def _validate_schema(parsed, schema):
    if schema is None:
        return True
    if not isinstance(parsed, dict):
        return False
    try:
        required = schema.get("required") or []
        for key in required:
            if key not in parsed:
                return False
        return True
    except Exception:
        return isinstance(parsed, dict)


def _classify_error(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection_error"
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status == 408:
            return "timeout"
        if status == 425:
            return "server_error"
        if status == 429:
            return "rate_limited"
        if status == 404:
            return "model_unavailable"
        if status and 400 <= status < 500:
            # Don't retry most client errors
            if status in (400, 422):
                return "bad_request"
            if status in (401, 403):
                return "auth_error"
            return "client_error"
        if status and status >= 500:
            return "server_error"
        return "http_error"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, KeyError):
        return "missing_fields"
    if isinstance(exc, TypeError):
        return "type_error"
    if isinstance(exc, LocalModelError):
        msg = str(exc)
        if "schema" in msg.lower():
            return "schema_invalid"
        if "json object" in msg.lower():
            return "invalid_json"
        return "local_model_error"
    return exc.__class__.__name__.lower()


def _is_retryable_error(exc):
    category = _classify_error(exc)
    retryable = {
        "timeout",
        "connection_error",
        "server_error",
        "model_unavailable",
        "rate_limited",
        "invalid_json",
        "missing_fields",
        "type_error",
        "schema_invalid",
        "local_model_error",
    }
    non_retryable = {
        "bad_request",
        "auth_error",
        "client_error",
    }
    if category in non_retryable:
        return False
    # Be conservative: don't retry arbitrary ValueError not from model output
    if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
        return False
    if isinstance(exc, (SyntaxError, AttributeError)):
        return False
    return category in retryable


def chat_json(
    messages,
    schema,
    *,
    temperature=0.2,
    timeout=None,
    model=None,
    context_tokens=None,
    max_output_tokens=None,
    role=None,
):
    if _dummy_mode():
        if schema is RESUME_PROFILE_SCHEMA:
            return dict(_DUMMY_RESUME_PROFILE)
        raise LocalModelError(
            "DUMMY_MODE: local model disabled; using deterministic fallback."
        )

    url, api_style = _api_url()
    timeout = timeout or float(settings.MOCK_INTERVIEW.get("OLLAMA_TIMEOUT", 120))
    primary = model or configured_model(role or "interactive")
    candidates = _get_route_candidates(role=role, primary_model=primary)
    if not candidates:
        candidates = [primary] if primary else [configured_model(role or "interactive")]

    meta = {
        "role": role or "unknown",
        "primary_model": primary,
        "attempts": [],
        "successful_model": None,
        "fallback_used": False,
        "total_latency_ms": 0,
    }
    _set_request_meta(meta)

    last_error = None
    overall_start = time.monotonic()
    for idx, candidate in enumerate(candidates):
        if api_style == "ollama":
            payload = {
                "model": candidate,
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
                "model": candidate,
                "messages": messages,
                "stream": False,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
        attempt_start = time.monotonic()
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            if api_style == "ollama":
                content = body["message"]["content"]
            else:
                content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
            if not isinstance(parsed, dict):
                raise LocalModelError("Local model did not return a JSON object.")
            if not _validate_schema(parsed, schema):
                raise LocalModelError("Local model response failed schema validation.")
            attempt_ms = int((time.monotonic() - attempt_start) * 1000)
            meta["attempts"].append(
                {
                    "model": candidate,
                    "ok": True,
                    "latency_ms": attempt_ms,
                    "error_category": None,
                }
            )
            meta["successful_model"] = candidate
            meta["fallback_used"] = idx > 0
            meta["total_latency_ms"] = int((time.monotonic() - overall_start) * 1000)
            _set_request_meta(meta)
            if idx > 0 and role:
                logger.info(
                    "LLM route: task=%s primary=%s failed, backup=%s succeeded (latency_ms=%s)",
                    role,
                    primary,
                    candidate,
                    attempt_ms,
                )
            elif role:
                logger.debug(
                    "LLM route: task=%s primary=%s succeeded (latency_ms=%s)",
                    role,
                    candidate,
                    attempt_ms,
                )
            return parsed
        except Exception as exc:
            attempt_ms = int((time.monotonic() - attempt_start) * 1000)
            cat = _classify_error(exc)
            meta["attempts"].append(
                {
                    "model": candidate,
                    "ok": False,
                    "latency_ms": attempt_ms,
                    "error_category": cat,
                }
            )
            _set_request_meta(meta)
            last_error = exc
            if not _is_retryable_error(exc):
                logger.error(
                    "LLM route: non-retryable error for task=%s model=%s category=%s",
                    role,
                    candidate,
                    cat,
                )
                break
            if idx == 0 and len(candidates) > 1 and role:
                logger.warning(
                    "LLM route: task=%s primary=%s failed category=%s, trying backup",
                    role,
                    candidate,
                    cat,
                )
            elif role and len(candidates) > 1:
                logger.warning(
                    "LLM route: task=%s model=%s failed category=%s, trying next backup",
                    role,
                    candidate,
                    cat,
                )
            continue
    meta["total_latency_ms"] = int((time.monotonic() - overall_start) * 1000)
    _set_request_meta(meta)
    if last_error is not None:
        raise LocalModelError(f"Local model request failed: {last_error}") from last_error
    raise LocalModelError("Local model request failed.")


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
