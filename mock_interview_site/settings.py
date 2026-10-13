"""Standalone settings for the Mock Interview fork.

This project runs the original ``mock_interview`` app outside the ERP. It uses
a local SQLite database and two local stub apps (``user_accounts`` and
``faculty_management``) that provide the small model surface the app needs.

``MOCK_INTERVIEW['DUMMY_MODE']`` makes every inference seam (LLM / STT / TTS /
RAG) run fully offline, so the site works with no Ollama, Whisper, Kokoro,
Piper or Qdrant running.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "MOCK_INTERVIEW_SECRET_KEY",
    "dev-only-insecure-key-change-me-mock-interview-fork",
)

DEBUG = os.environ.get("MOCK_INTERVIEW_DEBUG", "true").lower() in {"1", "true", "yes"}

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "user_accounts",
    "faculty_management",
    "mock_interview",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mock_interview_site.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "mock_interview_site.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_USER_MODEL = "user_accounts.USER"

AUTHENTICATION_BACKENDS = [
    "user_accounts.auth.EmployeeIDBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login_view"
LOGIN_REDIRECT_URL = "mock_interview:dashboard"
LOGOUT_REDIRECT_URL = "login_view"

X_FRAME_OPTIONS = "ALLOWALL"

# ───────────────────────────────────────────────────────────────────────────
# Mock Interview configuration (ported from the ERP settings.py).
# ───────────────────────────────────────────────────────────────────────────
MOCK_INTERVIEW = {
    "DUMMY_MODE": os.environ.get(
        "MOCK_INTERVIEW_DUMMY_MODE", "true"
    ).lower() in {"1", "true", "yes"},
    "OLLAMA_BASE_URL": os.environ.get(
        "MOCK_INTERVIEW_OLLAMA_BASE_URL", "http://127.0.0.1:11434"
    ),
    "OLLAMA_MODEL": os.environ.get("MOCK_INTERVIEW_OLLAMA_MODEL", "qwen3:8b"),
    "OLLAMA_INTERACTIVE_MODEL": os.environ.get(
        "MOCK_INTERVIEW_OLLAMA_INTERACTIVE_MODEL", "qwen3:1.7b"
    ),
    "OLLAMA_REPORT_MODEL": os.environ.get(
        "MOCK_INTERVIEW_OLLAMA_REPORT_MODEL", "qwen3:1.7b"
    ),
    "OLLAMA_TIMEOUT": float(os.environ.get("MOCK_INTERVIEW_OLLAMA_TIMEOUT", "120")),
    "OLLAMA_CONTEXT_TOKENS": int(
        os.environ.get("MOCK_INTERVIEW_OLLAMA_CONTEXT_TOKENS", "4096")
    ),
    "OLLAMA_MAX_OUTPUT_TOKENS": int(
        os.environ.get("MOCK_INTERVIEW_OLLAMA_MAX_OUTPUT_TOKENS", "1024")
    ),
    "OLLAMA_THINK": os.environ.get(
        "MOCK_INTERVIEW_OLLAMA_THINK", "false"
    ).lower() in {"1", "true", "yes"},
    "WHISPER_MODEL": os.environ.get("MOCK_INTERVIEW_WHISPER_MODEL", "large-v3-turbo"),
    "WHISPER_DEVICE": os.environ.get("MOCK_INTERVIEW_WHISPER_DEVICE", "cpu"),
    "WHISPER_COMPUTE_TYPE": os.environ.get(
        "MOCK_INTERVIEW_WHISPER_COMPUTE_TYPE", "int8"
    ),
    "WHISPER_BEAM_SIZE": int(os.environ.get("MOCK_INTERVIEW_WHISPER_BEAM_SIZE", "1")),
    "WHISPER_REPETITION_PENALTY": float(
        os.environ.get("MOCK_INTERVIEW_WHISPER_REPETITION_PENALTY", "1.15")
    ),
    "WHISPER_NO_REPEAT_NGRAM_SIZE": int(
        os.environ.get("MOCK_INTERVIEW_WHISPER_NO_REPEAT_NGRAM_SIZE", "4")
    ),
    "KOKORO_VOICE": os.environ.get("MOCK_INTERVIEW_KOKORO_VOICE", "af_heart"),
    "KOKORO_ONNX_MODEL": os.environ.get(
        "MOCK_INTERVIEW_KOKORO_ONNX_MODEL",
        str(BASE_DIR / "mock_interview" / "model_data" / "kokoro" / "kokoro-v1.0.onnx"),
    ),
    "KOKORO_ONNX_VOICES": os.environ.get(
        "MOCK_INTERVIEW_KOKORO_ONNX_VOICES",
        str(BASE_DIR / "mock_interview" / "model_data" / "kokoro" / "voices-v1.0.bin"),
    ),
    "TTS_BACKEND": os.environ.get("MOCK_INTERVIEW_TTS_BACKEND", "auto"),
    "PIPER_VOICE_MODEL": os.environ.get(
        "MOCK_INTERVIEW_PIPER_VOICE_MODEL",
        str(
            BASE_DIR
            / "mock_interview"
            / "model_data"
            / "piper"
            / "en_US-lessac-medium.onnx"
        ),
    ),
    "TTS_SPEED": float(os.environ.get("MOCK_INTERVIEW_TTS_SPEED", "0.95")),
    "MAX_RESUME_BYTES": int(
        os.environ.get("MOCK_INTERVIEW_MAX_RESUME_BYTES", str(5 * 1024 * 1024))
    ),
    "MAX_ANSWER_AUDIO_BYTES": int(
        os.environ.get("MOCK_INTERVIEW_MAX_ANSWER_AUDIO_BYTES", str(25 * 1024 * 1024))
    ),
    "ALLOW_DETERMINISTIC_FALLBACK": os.environ.get(
        "MOCK_INTERVIEW_ALLOW_DETERMINISTIC_FALLBACK", "true"
    ).lower() in {"1", "true", "yes"},
    "FAST_INTERVIEW_PIPELINE": os.environ.get(
        "MOCK_INTERVIEW_FAST_INTERVIEW_PIPELINE", "true"
    ).lower() in {"1", "true", "yes"},
    "LATENCY_LOG": os.environ.get(
        "MOCK_INTERVIEW_LATENCY_LOG", "true"
    ).lower() in {"1", "true", "yes"},
    "INTERVIEW_WORKER_AUTO_START": os.environ.get(
        "MOCK_INTERVIEW_INTERVIEW_WORKER_AUTO_START", "true"
    ).lower() in {"1", "true", "yes"},
    "INTERVIEW_WORKER_INTERVAL": float(
        os.environ.get("MOCK_INTERVIEW_INTERVIEW_WORKER_INTERVAL", "2.0")
    ),
    "INTERVIEW_EVAL_STALE_SECONDS": float(
        os.environ.get("MOCK_INTERVIEW_INTERVIEW_EVAL_STALE_SECONDS", "600")
    ),
    "INTERVIEW_EVAL_MAX_ATTEMPTS": int(
        os.environ.get("MOCK_INTERVIEW_INTERVIEW_EVAL_MAX_ATTEMPTS", "3")
    ),
    "TTS_CACHE_ENABLED": os.environ.get(
        "MOCK_INTERVIEW_TTS_CACHE_ENABLED", "true"
    ).lower() in {"1", "true", "yes"},
    "TTS_CACHE_DIR": os.environ.get("MOCK_INTERVIEW_TTS_CACHE_DIR", "tts_cache"),
}

# RAG pipeline configuration (ported from the ERP settings.py).
RAG_CONFIG = {
    "QDRANT_URL": os.environ.get("RAG_QDRANT_URL", "http://127.0.0.1:6333"),
    "QDRANT_COLLECTION": os.environ.get(
        "RAG_QDRANT_COLLECTION", "mock_interview_documents"
    ),
    "EMBEDDING_MODEL": os.environ.get("RAG_EMBEDDING_MODEL", "BAAI/bge-m3"),
    "EMBEDDING_DIMENSION": int(os.environ.get("RAG_EMBEDDING_DIMENSION", "1024")),
    "CHUNK_SIZE": int(os.environ.get("RAG_CHUNK_SIZE", "512")),
    "CHUNK_OVERLAP": int(os.environ.get("RAG_CHUNK_OVERLAP", "64")),
    "TOP_K_CHUNKS": int(os.environ.get("RAG_TOP_K_CHUNKS", "5")),
    "MAX_DOCUMENT_BYTES": int(
        os.environ.get("RAG_MAX_DOCUMENT_BYTES", str(50 * 1024 * 1024))
    ),
    "ALLOWED_DOCUMENT_TYPES": [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    ],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
