"""Isolated settings for mock-interview unit tests.

Import the standalone project settings so the tests run without the ERP.
"""

from mock_interview_site.settings import *  # noqa: F403


DATABASES = {  # noqa: F405
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# The fast interview pipeline (pre-planned pool + background evaluation) is
# exercised by its own test module (test_fast_pipeline.py) with the flag ON.
# Keep it OFF here so legacy unit tests exercise the original synchronous
# submit -> evaluate -> question path unchanged.
MOCK_INTERVIEW = dict(MOCK_INTERVIEW)  # noqa: F405
MOCK_INTERVIEW["FAST_INTERVIEW_PIPELINE"] = False
MOCK_INTERVIEW["LATENCY_LOG"] = False
MOCK_INTERVIEW["INTERVIEW_WORKER_AUTO_START"] = False
MOCK_INTERVIEW["TTS_CACHE_ENABLED"] = False
