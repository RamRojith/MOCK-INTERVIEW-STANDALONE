import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


def _dummy_stt():
    config = settings.MOCK_INTERVIEW
    return bool(config.get("DUMMY_STT", config.get("DUMMY_MODE", False)))


class MockInterviewConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mock_interview'

    def ready(self):
        if _dummy_stt():
            # The canned transcript keeps the site usable with no Whisper
            # install; the background worker still runs either way.
            logger.info(
                "STT dummy mode enabled: skipping Whisper pre-load."
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
