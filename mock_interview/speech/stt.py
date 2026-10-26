import logging
from functools import lru_cache
from collections import Counter
from pathlib import Path
import re

from django.conf import settings

logger = logging.getLogger(__name__)


class SpeechToTextError(RuntimeError):
    pass


def _dummy_mode():
    """True when speech-to-text should return the canned transcript.

    ``DUMMY_STT`` can be toggled on its own: the offline LLM/TTS stand-ins can
    stay enabled while real speech recognition runs, and vice versa.
    """
    config = getattr(settings, "MOCK_INTERVIEW", {})
    return bool(config.get("DUMMY_STT", config.get("DUMMY_MODE", False)))


# Canned transcript used when DUMMY_MODE is on so the whole interview works
# offline. It is deliberately generic but substantive, so speech metrics and
# reports render sensibly.
_DUMMY_TRANSCRIPT = (
    "In my project I built a Django web application with user authentication "
    "and role based dashboards. I used a relational database to store the "
    "records, wrote the API endpoints, and added automated tests. I handled "
    "edge cases by validating the input, logging errors, and writing clear "
    "documentation. I also worked with my team using version control and "
    "regular code reviews, which helped me improve the overall design and keep "
    "the code maintainable."
)


def _dummy_words(transcript):
    words = []
    cursor = 0.0
    for token in transcript.split():
        start = cursor
        end = round(cursor + 0.35, 3)
        words.append(
            {
                "word": token,
                "start": round(cursor, 3),
                "end": round(end, 3),
                "probability": 0.92,
            }
        )
        cursor += 0.4
    return words


@lru_cache(maxsize=2)
def _load_model(model_name, device, compute_type):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SpeechToTextError(
            "Faster Whisper is not installed. Install the documented open-source "
            "speech dependencies before transcribing audio."
        ) from exc
    try:
        logger.info(
            "Loading Faster-Whisper model=%s device=%s compute_type=%s",
            model_name,
            device,
            compute_type,
        )
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        # Model download/load failures must surface as a SPEECH error, not as an
        # unhandled 500: the room turns this into a readable 503 and lets the
        # student retry instead of silently losing the recorded answer.
        logger.exception(
            "Faster-Whisper model %s could not be loaded on %s",
            model_name,
            device,
        )
        raise SpeechToTextError(
            f"Speech recognition model '{model_name}' could not be loaded "
            f"on {device}: {exc}"
        ) from exc
    logger.info("Faster-Whisper model=%s is ready", model_name)
    return model


def _audio_context(audio_source):
    """Best-effort audio metadata for logs (never raises, never consumes input)."""
    context = {
        "source": type(audio_source).__name__,
        "container": None,
        "sample_rate": None,
        "channels": None,
        "size_bytes": None,
    }
    if not hasattr(audio_source, "read"):
        try:
            context["size_bytes"] = Path(str(audio_source)).stat().st_size
        except OSError:
            pass
        return context

    try:
        position = audio_source.tell()
    except Exception:
        position = None
    try:
        header = audio_source.read(64)
        if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
            context["container"] = "wav"
            context["channels"] = int.from_bytes(header[22:24], "little")
            context["sample_rate"] = int.from_bytes(header[24:28], "little")
        elif header[:4] == b"\x1a\x45\xdf\xa3":
            context["container"] = "webm"
        elif header[:4] == b"OggS":
            context["container"] = "ogg"
        elif len(header) >= 12 and header[4:8] == b"ftyp":
            context["container"] = "mp4"
        elif header.startswith(b"ID3"):
            context["container"] = "mp3"
        get_size = getattr(audio_source, "size", None)
        if isinstance(get_size, int):
            context["size_bytes"] = get_size
    except Exception:
        pass
    try:
        audio_source.seek(0 if position is None else position)
    except Exception:
        pass
    return context


def preload_whisper_model():
    """Pre-load the Whisper model at startup to avoid cold-start latency."""
    if _dummy_mode():
        logger.info("STT dummy mode: Whisper is never loaded.")
        return
    config = settings.MOCK_INTERVIEW
    model_name = config.get("WHISPER_MODEL", "large-v3-turbo")
    device = config.get("WHISPER_DEVICE", "cpu")
    compute_type = config.get("WHISPER_COMPUTE_TYPE", "int8")
    _load_model(model_name, device, compute_type)



def assess_transcript_quality(transcript, duration_seconds, segment_list):
    """Flag likely hallucinations without trying to invent replacement text."""
    words = re.findall(r"[a-z0-9']+", (transcript or "").lower())
    issues = []
    duration = max(float(duration_seconds or 0), 1.0)
    words_per_minute = round(len(words) * 60 / duration, 1)

    ngrams = [
        tuple(words[index : index + 4])
        for index in range(max(0, len(words) - 3))
    ]
    maximum_repeat = max(Counter(ngrams).values(), default=0)
    unique_ngram_ratio = (
        round(len(set(ngrams)) / len(ngrams), 3) if ngrams else 1.0
    )
    log_probabilities = [
        float(segment.avg_logprob)
        for segment in segment_list
        if getattr(segment, "avg_logprob", None) is not None
    ]
    average_log_probability = (
        round(sum(log_probabilities) / len(log_probabilities), 3)
        if log_probabilities
        else None
    )

    if not words:
        issues.append("No recognizable speech was found.")
    if len(words) >= 20 and (maximum_repeat >= 3 or unique_ngram_ratio < 0.55):
        issues.append(
            "The transcript contains unlikely repeated phrases."
        )
    if len(words) >= 20 and words_per_minute > 240:
        issues.append(
            "The transcript contains more words than the recording can plausibly hold."
        )
    if (
        average_log_probability is not None
        and average_log_probability < -1.0
    ):
        issues.append(
            "The speech decoder had low confidence in the recognized wording."
        )

    return {
        "status": "needs_review" if issues else "ok",
        "issues": issues,
        "words_per_minute": words_per_minute,
        "maximum_repeated_four_word_phrase": maximum_repeat,
        "unique_four_word_phrase_ratio": unique_ngram_ratio,
        "average_log_probability": average_log_probability,
    }


def transcribe_audio(path, language_mode="en"):
    config = settings.MOCK_INTERVIEW
    if _dummy_mode():
        logger.info(
            "STT dummy mode: returning the canned transcript instead of decoding "
            "the uploaded audio (container/sample rate are not inspected)."
        )
        words = _dummy_words(_DUMMY_TRANSCRIPT)
        duration = max((word["end"] for word in words), default=0.0)
        return {
            "transcript": _DUMMY_TRANSCRIPT,
            "detected_language": {"en": "en", "ta": "ta"}.get(
                language_mode, "en"
            ),
            "confidence": 0.95,
            "words": words,
            "duration_seconds": int(round(duration)),
            "model_name": "dummy-transcriber",
            "quality": {
                "status": "ok",
                "issues": [],
                "words_per_minute": round(
                    len(words) * 60 / max(duration, 1.0), 1
                ),
                "maximum_repeated_four_word_phrase": 1,
                "unique_four_word_phrase_ratio": 1.0,
                "average_log_probability": None,
            },
        }
    model_name = config.get("WHISPER_MODEL", "large-v3-turbo")
    device = config.get("WHISPER_DEVICE", "cpu")
    compute_type = config.get("WHISPER_COMPUTE_TYPE", "int8")
    model = _load_model(model_name, device, compute_type)
    language = {"en": "en", "ta": "ta"}.get(language_mode)
    audio_source = path if hasattr(path, "read") else str(path)
    audio_info = _audio_context(audio_source)
    if hasattr(audio_source, "seek"):
        try:
            audio_source.seek(0)
        except Exception:
            pass
    logger.info(
        "STT inference start: model=%s language=%s requested_mode=%s audio=%s",
        model_name,
        language or "auto",
        language_mode,
        audio_info,
    )
    try:
        segments, info = model.transcribe(
            audio_source,
            language=language,
            beam_size=int(config.get("WHISPER_BEAM_SIZE", 1)),
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 250,
            },
            word_timestamps=True,
            temperature=0,
            condition_on_previous_text=False,
            repetition_penalty=float(
                config.get("WHISPER_REPETITION_PENALTY", 1.15)
            ),
            no_repeat_ngram_size=int(
                config.get("WHISPER_NO_REPEAT_NGRAM_SIZE", 4)
            ),
            compression_ratio_threshold=2.0,
            log_prob_threshold=-0.8,
            no_speech_threshold=0.6,
            hallucination_silence_threshold=1.0,
        )
        segment_list = list(segments)
    except Exception as exc:
        logger.exception(
            "STT inference failed: model=%s audio=%s", model_name, audio_info
        )
        raise SpeechToTextError(f"Audio transcription failed: {exc}") from exc

    transcript = " ".join(segment.text.strip() for segment in segment_list).strip()
    words = []
    for segment in segment_list:
        for word in segment.words or []:
            words.append(
                {
                    "word": word.word.strip(),
                    "start": round(float(word.start), 3),
                    "end": round(float(word.end), 3),
                    "probability": round(float(word.probability), 4),
                }
            )
    probabilities = [word["probability"] for word in words]
    confidence = (
        round(sum(probabilities) / len(probabilities), 4)
        if probabilities
        else None
    )
    duration = max((float(segment.end) for segment in segment_list), default=0)
    quality = assess_transcript_quality(
        transcript,
        duration,
        segment_list,
    )
    logger.info(
        "STT inference done: model=%s language=%s chars=%d words=%d "
        "duration=%.1fs confidence=%s container=%s sample_rate=%s quality=%s",
        model_name,
        getattr(info, "language", language or ""),
        len(transcript),
        len(words),
        duration,
        confidence,
        audio_info.get("container"),
        audio_info.get("sample_rate"),
        quality.get("status"),
    )
    if not transcript:
        logger.warning(
            "STT returned an empty transcript (audio=%s). The student must "
            "re-record or type the answer.",
            audio_info,
        )
    return {
        "transcript": transcript,
        "detected_language": getattr(info, "language", language or ""),
        "confidence": confidence,
        "words": words,
        "duration_seconds": int(round(duration)),
        "model_name": model_name,
        "quality": quality,
    }
