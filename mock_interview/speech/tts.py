import array
import hashlib
import importlib.util
import io
import logging
import sys
import threading
import time
import wave
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

# The sample is deliberately short: the device check uses it to prove that the
# configured backend can synthesize real audio, and the room serves the same
# cached bytes so the browser can prove it can play them.
TTS_PROBE_TEXT = "Hello. This is your interviewer speaking."
_SUPPORTED_BACKENDS = {"kokoro", "kokoro_onnx", "piper"}
_BACKEND_LABELS = {
    "kokoro": "kokoro",
    "kokoro_onnx": "kokoro-onnx",
    "piper": "piper",
}

# ONNX Runtime sessions tolerate concurrent inference, but the phonemizer and
# the model cache are safer when only one synthesis runs at a time. This also
# prevents the startup warm-up and a room request from both paying for the
# expensive first inference.
_synthesis_lock = threading.Lock()
_probe_lock = threading.Lock()


class TextToSpeechError(RuntimeError):
    pass


def _dummy_mode():
    return bool(
        settings.MOCK_INTERVIEW.get(
            "DUMMY_TTS",
            settings.MOCK_INTERVIEW.get("DUMMY_MODE", False),
        )
    )


def _dummy_wav_bytes(seconds=2.0, sample_rate=24000):
    """A short silence WAV so the audio player always has a valid source."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buffer.getvalue()


def _wav_metadata(audio_bytes):
    """Return basic PCM WAV metadata, raising for anything unplayable."""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            raw = wav_file.readframes(frame_count)
    except (wave.Error, EOFError, OSError) as exc:
        raise TextToSpeechError(f"TTS produced an invalid WAV file: {exc}") from exc

    if sample_rate <= 0 or frame_count <= 0:
        raise TextToSpeechError("TTS produced an empty WAV file.")
    if sample_width not in (1, 2, 4):
        raise TextToSpeechError(
            f"TTS produced an unsupported {sample_width * 8}-bit WAV file."
        )

    if sample_width == 1:
        samples = array.array("B")
    elif sample_width == 2:
        samples = array.array("h")
    else:
        samples = array.array("i")
    usable = len(raw) - (len(raw) % sample_width)
    samples.frombytes(raw[:usable])
    if sys.byteorder == "big" and sample_width > 1:
        samples.byteswap()

    if sample_width == 1:
        peak = max((abs(value - 128) for value in samples), default=0)
        full_scale = 128
    else:
        peak = max(max(samples), -min(samples)) if samples else 0
        full_scale = 2 ** (sample_width * 8 - 1)

    duration = frame_count / float(sample_rate)
    return {
        "duration_seconds": round(duration, 3),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "frames": frame_count,
        "peak": int(peak),
        "peak_ratio": round(peak / float(full_scale), 4),
        "bytes": len(audio_bytes),
    }


def validate_audio_bytes(audio_bytes, minimum_seconds=0.25):
    """Reject empty, malformed, silent or suspiciously short TTS output."""
    metadata = _wav_metadata(audio_bytes)
    if metadata["duration_seconds"] < minimum_seconds:
        raise TextToSpeechError(
            "TTS produced only "
            f"{metadata['duration_seconds']:.2f}s of audio; at least "
            f"{minimum_seconds:.2f}s is required."
        )
    if metadata["peak"] <= 0:
        raise TextToSpeechError(
            "TTS produced silent audio. Install a real local TTS backend or "
            "disable DUMMY_TTS."
        )
    return metadata


@lru_cache(maxsize=4)
def _kokoro_pipeline(language_code):
    try:
        from kokoro_onnx import KPipeline
    except ImportError as exc:
        raise TextToSpeechError(
            "Kokoro is not installed. Install the documented open-source TTS "
            "dependencies before generating interviewer audio."
        ) from exc
    started = time.monotonic()
    try:
        pipeline = KPipeline(lang_code=language_code)
    except Exception as exc:
        raise TextToSpeechError(f"Kokoro could not be loaded: {exc}") from exc
    logger.info(
        "Kokoro pipeline loaded (lang=%s) in %.2fs",
        language_code,
        time.monotonic() - started,
    )
    return pipeline


def _kokoro_onnx_paths():
    return (
        Path(settings.MOCK_INTERVIEW.get("KOKORO_ONNX_MODEL", "")),
        Path(settings.MOCK_INTERVIEW.get("KOKORO_ONNX_VOICES", "")),
    )


def _kokoro_onnx_ready():
    model_path, voices_path = _kokoro_onnx_paths()
    return (
        importlib.util.find_spec("kokoro_onnx") is not None
        and model_path.is_file()
        and voices_path.is_file()
    )


@lru_cache(maxsize=1)
def _kokoro_onnx_model(model_path, voices_path):
    try:
        from kokoro_onnx import Kokoro
    except ImportError as exc:
        raise TextToSpeechError(
            "Kokoro ONNX is not installed for this Python runtime."
        ) from exc
    started = time.monotonic()
    try:
        model = Kokoro(model_path, voices_path)
    except Exception as exc:
        raise TextToSpeechError(f"Kokoro ONNX could not be loaded: {exc}") from exc
    logger.info(
        "Kokoro ONNX model loaded in %.2fs (model=%s voices=%s)",
        time.monotonic() - started,
        model_path,
        voices_path,
    )
    return model


def _selected_backend():
    configured = settings.MOCK_INTERVIEW.get("TTS_BACKEND", "auto").lower()
    if configured != "auto":
        if configured == "kokoro" and importlib.util.find_spec("kokoro") is None:
            return "kokoro_onnx" if _kokoro_onnx_ready() else "kokoro"
        return configured
    if importlib.util.find_spec("kokoro") is not None:
        return "kokoro"
    if _kokoro_onnx_ready():
        return "kokoro_onnx"
    if importlib.util.find_spec("piper") is not None:
        return "piper"
    return "unavailable"


def _backend_label(backend):
    return _BACKEND_LABELS.get(backend, backend)


def _probe_voice():
    return settings.MOCK_INTERVIEW.get("KOKORO_VOICE", "af_heart")


def _probe_speed():
    return float(settings.MOCK_INTERVIEW.get("TTS_SPEED", 0.95))


@lru_cache(maxsize=4)
def _synthesize_probe(backend, voice, speed):
    started = time.monotonic()
    logger.info(
        "TTS probe starting (backend=%s voice=%s speed=%s text=%r)",
        _backend_label(backend),
        voice,
        speed,
        TTS_PROBE_TEXT,
    )
    if backend == "kokoro":
        audio_bytes = _synthesize_kokoro(TTS_PROBE_TEXT, voice)
    elif backend == "kokoro_onnx":
        audio_bytes = _synthesize_kokoro_onnx(TTS_PROBE_TEXT, voice)
    elif backend == "piper":
        audio_bytes = _synthesize_piper(TTS_PROBE_TEXT)
    else:
        raise TextToSpeechError("No supported local TTS backend is installed.")
    metadata = validate_audio_bytes(audio_bytes)
    logger.info(
        "TTS probe ready (backend=%s elapsed=%.2fs bytes=%s duration=%.2fs "
        "sample_rate=%s peak_ratio=%s)",
        _backend_label(backend),
        time.monotonic() - started,
        metadata["bytes"],
        metadata["duration_seconds"],
        metadata["sample_rate"],
        metadata["peak_ratio"],
    )
    return audio_bytes, metadata


def synthesize_probe():
    """Synthesize (or return the cached) short voice sample."""
    if _dummy_mode():
        raise TextToSpeechError(
            "TTS is running with the silent offline stand-in. Set "
            "MOCK_INTERVIEW_DUMMY_TTS=false and install a local TTS engine."
        )
    backend = _selected_backend()
    if backend not in _SUPPORTED_BACKENDS:
        raise TextToSpeechError(
            "No supported local TTS backend is installed. Install kokoro-onnx "
            "or Piper and configure the model files."
        )
    with _probe_lock:
        return _synthesize_probe(backend, _probe_voice(), _probe_speed())


def warm_up_tts():
    """Load and exercise the backend in the background worker."""
    if _dummy_mode():
        logger.info("TTS dummy mode enabled: skipping voice warm-up.")
        return None
    backend = _selected_backend()
    if backend not in _SUPPORTED_BACKENDS:
        logger.warning(
            "TTS warm-up skipped: no supported backend is installed."
        )
        return None
    logger.info("Warming up TTS backend %s...", _backend_label(backend))
    started = time.monotonic()
    try:
        _, metadata = synthesize_probe()
    except TextToSpeechError as exc:
        logger.warning("TTS warm-up failed: %s", exc)
        return None
    logger.info(
        "TTS warm-up complete (backend=%s elapsed=%.2fs duration=%.2fs)",
        _backend_label(backend),
        time.monotonic() - started,
        metadata["duration_seconds"],
    )
    return metadata


def tts_status():
    if _dummy_mode():
        return {
            "engine": "dummy",
            "ready": False,
            "synthesis": False,
            "dummy": True,
            "detail": (
                "TTS is running with the silent offline stand-in. Set "
                "MOCK_INTERVIEW_DUMMY_TTS=false and configure a real engine."
            ),
        }

    backend = _selected_backend()
    engine = _backend_label(backend)
    if backend not in _SUPPORTED_BACKENDS:
        return {
            "engine": engine,
            "ready": False,
            "synthesis": False,
            "dummy": False,
            "detail": (
                "No supported local TTS backend is installed. Install "
                "kokoro-onnx or Piper and configure the model files."
            ),
        }

    try:
        _, metadata = synthesize_probe()
    except TextToSpeechError as exc:
        logger.warning("TTS self-check failed (%s): %s", engine, exc)
        return {
            "engine": engine,
            "ready": False,
            "synthesis": False,
            "dummy": False,
            "detail": str(exc),
        }
    return {
        "engine": engine,
        "ready": True,
        "synthesis": True,
        "dummy": False,
        "detail": "Synthesized a test sentence successfully.",
        "probe": metadata,
    }


def _synthesize_kokoro(text, voice):
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as exc:
        raise TextToSpeechError(
            "Kokoro audio generation requires numpy and soundfile."
        ) from exc

    pipeline = _kokoro_pipeline("a")
    try:
        chunks = []
        with _synthesis_lock:
            for _, _, audio in pipeline(
                text,
                voice=voice,
                speed=float(settings.MOCK_INTERVIEW.get("TTS_SPEED", 0.95)),
            ):
                chunks.append(np.asarray(audio, dtype=np.float32))
        if not chunks:
            raise TextToSpeechError("Kokoro returned no audio.")
        buffer = io.BytesIO()
        sf.write(buffer, np.concatenate(chunks), 24000, format="WAV")
    except TextToSpeechError:
        raise
    except Exception as exc:
        raise TextToSpeechError(f"Question speech generation failed: {exc}") from exc

    return buffer.getvalue()


def _synthesize_kokoro_onnx(text, voice):
    try:
        import soundfile as sf
    except ImportError as exc:
        raise TextToSpeechError(
            "Kokoro ONNX audio generation requires soundfile."
        ) from exc
    model_path, voices_path = _kokoro_onnx_paths()
    if not _kokoro_onnx_ready():
        raise TextToSpeechError(
            "Kokoro ONNX model or voice files are missing."
        )
    try:
        model = _kokoro_onnx_model(str(model_path), str(voices_path))
        with _synthesis_lock:
            samples, sample_rate = model.create(
                text,
                voice=voice,
                speed=float(settings.MOCK_INTERVIEW.get("TTS_SPEED", 0.95)),
                lang="en-us",
            )
        buffer = io.BytesIO()
        sf.write(buffer, samples, sample_rate, format="WAV")
    except TextToSpeechError:
        raise
    except Exception as exc:
        raise TextToSpeechError(
            f"Kokoro ONNX speech generation failed: {exc}"
        ) from exc
    return buffer.getvalue()


@lru_cache(maxsize=2)
def _piper_voice(model_path):
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise TextToSpeechError(
            "Piper TTS is not installed for this Python runtime."
        ) from exc
    return PiperVoice.load(model_path)


def _synthesize_piper(text):
    model_path = Path(
        settings.MOCK_INTERVIEW.get("PIPER_VOICE_MODEL", "")
    )
    if not model_path.is_file() or not Path(f"{model_path}.json").is_file():
        raise TextToSpeechError(
            "Piper voice files are missing. Configure "
            "MOCK_INTERVIEW_PIPER_VOICE_MODEL."
        )
    buffer = io.BytesIO()
    try:
        with _synthesis_lock:
            with wave.open(buffer, "wb") as wav_file:
                _piper_voice(str(model_path)).synthesize_wav(text, wav_file)
    except Exception as exc:
        raise TextToSpeechError(f"Piper speech generation failed: {exc}") from exc
    return buffer.getvalue()


def _tts_cache_dir():
    configured = settings.MOCK_INTERVIEW.get("TTS_CACHE_DIR", "tts_cache")
    return Path(configured) if Path(configured).is_absolute() else (
        Path(settings.MEDIA_ROOT) / configured
    )


def _tts_cache_key(text, voice, language, speed, backend):
    raw = "\x00".join(
        [str(text), str(voice), str(language), str(speed), str(backend)]
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:40]


def _tts_cache_path(text, voice, language, speed, backend):
    return _tts_cache_dir() / f"{_tts_cache_key(text, voice, language, speed, backend)}.wav"


def _tts_cache_enabled():
    return bool(settings.MOCK_INTERVIEW.get("TTS_CACHE_ENABLED", True))


def _read_tts_cache(cache_path):
    try:
        if cache_path.is_file():
            return cache_path.read_bytes()
    except OSError:
        return None
    return None


def _write_tts_cache(cache_path, audio_bytes):
    if not _tts_cache_enabled():
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(audio_bytes)
    except OSError as exc:
        logger.warning("Could not write TTS cache %s: %s", cache_path, exc)


def synthesize_question(question):
    if question.audio_file:
        return question.audio_file
    if _dummy_mode():
        logger.warning(
            "TTS dummy mode is enabled for question %s; the generated WAV is "
            "silent and will not be spoken.",
            question.public_id,
        )
        question.audio_file.save(
            f"question-{question.public_id}.wav",
            ContentFile(_dummy_wav_bytes()),
            save=True,
        )
        return question.audio_file
    if question.session.language_mode != "en":
        raise TextToSpeechError(
            "Tamil TTS requires the separately deployed AI4Bharat Indic-TTS service."
        )
    backend = _selected_backend()
    if backend not in _SUPPORTED_BACKENDS:
        raise TextToSpeechError(
            "No supported local TTS backend is installed."
        )
    voice = question.session.interviewer_voice or settings.MOCK_INTERVIEW.get(
        "KOKORO_VOICE", "af_heart"
    )
    speed = float(settings.MOCK_INTERVIEW.get("TTS_SPEED", 0.95))
    cache_path = None
    if _tts_cache_enabled():
        cache_path = _tts_cache_path(
            question.question_text,
            voice,
            question.session.language_mode,
            speed,
            backend,
        )
        cached = _read_tts_cache(cache_path)
        if cached is not None:
            try:
                metadata = validate_audio_bytes(cached)
            except TextToSpeechError as exc:
                logger.warning(
                    "Discarding invalid TTS cache %s: %s", cache_path, exc
                )
                cached = None
            if cached is not None:
                logger.info(
                    "TTS cache hit for question %s (backend=%s bytes=%s "
                    "duration=%.2fs)",
                    question.public_id,
                    _backend_label(backend),
                    metadata["bytes"],
                    metadata["duration_seconds"],
                )
                question.audio_file.save(
                    f"question-{question.public_id}.wav",
                    ContentFile(cached),
                    save=True,
                )
                return question.audio_file

    started = time.monotonic()
    logger.info(
        "TTS synthesis starting for question %s (backend=%s voice=%s chars=%s)",
        question.public_id,
        _backend_label(backend),
        voice,
        len(question.question_text),
    )
    try:
        if backend == "kokoro":
            audio_bytes = _synthesize_kokoro(question.question_text, voice)
        elif backend == "kokoro_onnx":
            audio_bytes = _synthesize_kokoro_onnx(question.question_text, voice)
        else:
            audio_bytes = _synthesize_piper(question.question_text)
    except TextToSpeechError:
        logger.warning(
            "TTS synthesis failed for question %s (backend=%s)",
            question.public_id,
            _backend_label(backend),
            exc_info=True,
        )
        raise

    metadata = validate_audio_bytes(audio_bytes)
    logger.info(
        "TTS synthesis ready for question %s (backend=%s elapsed=%.2fs "
        "bytes=%s duration=%.2fs sample_rate=%s peak_ratio=%s)",
        question.public_id,
        _backend_label(backend),
        time.monotonic() - started,
        metadata["bytes"],
        metadata["duration_seconds"],
        metadata["sample_rate"],
        metadata["peak_ratio"],
    )

    if cache_path is not None:
        _write_tts_cache(cache_path, audio_bytes)
    question.audio_file.save(
        f"question-{question.public_id}.wav",
        ContentFile(audio_bytes),
        save=True,
    )
    return question.audio_file
