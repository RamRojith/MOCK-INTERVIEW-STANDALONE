import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


def _dummy_mode():
    return bool(settings.MOCK_INTERVIEW.get("DUMMY_MODE", False))


class MockInterviewConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mock_interview'

    def ready(self):
        if _dummy_mode():
            # DUMMY_MODE runs fully offline: the Whisper model is never loaded
            # (STT is stubbed) but the background worker still runs so that
            # evaluations/reports are produced from the deterministic
            # fallbacks and the site behaves like the real one end-to-end.
            logger.info(
                "Mock interview DUMMY_MODE enabled: skipping Whisper pre-load."
            )
        else:
            from mock_interview.speech.stt import preload_whisper_model
            try:
                logger.info("Pre-loading Whisper model at startup...")
                preload_whisper_model()
                logger.info("Whisper model loaded successfully.")
            except Exception as exc:
                logger.warning("Could not pre-load Whisper model at startup: %s", exc)

        from mock_interview.services.interview_worker import ensure_auto_worker
        ensure_auto_worker()
