# Mock Interview — Standalone

A standalone Django project that runs the `mock_interview` app outside the ERP.
It ships with local stub apps (`user_accounts`, `faculty_management`), a local
SQLite database, and a **DUMMY_MODE** that makes every inference seam
(LLM / speech-to-text / text-to-speech / RAG) run fully offline. No Ollama,
Whisper, Kokoro, Piper or Qdrant is required.

## Quick start

```powershell
cd standalone
python manage.py migrate
python manage.py seed_dummy_data
python manage.py runserver
```

Open http://127.0.0.1:8000/ and log in at `/login/`.

### Demo logins (password: `demo@1234`)

| Role      | Employee ID | Name                 |
|-----------|-------------|----------------------|
| Superuser | `9999`      | System Admin         |
| Student   | `1001`      | Arjun Kumar          |
| Student   | `1002`      | Divya Sharma         |
| Faculty   | `2001`      | Prof. Meena R        |
| HOD       | `3001`      | Dr. Suresh Kumar     |
| Mentor    | `4001`      | Prof. Anil Varghese  |

## What the seed creates
- departments, roles, faculty profiles and a student roster
- a parsed resume + a generated ATS resume check (PDF + DOCX)
- a faculty teaching document with chunks (RAG source)
- a faculty-created interview with a student assignment
- two completed interviews with per-question evaluations, reports and PDFs

## DUMMY_MODE
`MOCK_INTERVIEW['DUMMY_MODE']` defaults to `True` (env `MOCK_INTERVIEW_DUMMY_MODE`).
When enabled:

* `ai/ollama_client.chat_json` serves resume normalization from a canned profile
  and raises `LocalModelError` for everything else, which activates the app's
  built-in deterministic fallbacks.
* `speech/stt.py` returns a canned transcript and never loads Whisper. This
  branch can be controlled on its own with `MOCK_INTERVIEW['DUMMY_STT']`
  (env `MOCK_INTERVIEW_DUMMY_STT`): it is off automatically as soon as
  `faster_whisper` is installed, and can be forced on for fully offline runs.
* `speech/tts.py` writes a short silent WAV instead of calling Kokoro/Piper.
  TTS can be switched independently with `MOCK_INTERVIEW['DUMMY_TTS']`
  (env `MOCK_INTERVIEW_DUMMY_TTS`), which defaults to `DUMMY_MODE`. Dummy
  audio is always reported as **not ready** by the device check; the room
  keeps recording locked when the question cannot be spoken.
* `rag/embeddings.py` + `rag/vectorstore.py` use deterministic pseudo-embeddings
  and an in-memory vector store.

Set `MOCK_INTERVIEW_DUMMY_MODE=false` to use the real local models/services.

## Real speech recognition (Faster-Whisper)

Installing the engine is enough to switch STT to real transcription; the LLM
and TTS stand-ins keep working:

```powershell
python -m pip install faster-whisper
```

The model is downloaded from Hugging Face on first use. The default is
`large-v3-turbo`. On CPU-only machines `small` is a good trade-off between
accuracy and speed:

```powershell
$env:MOCK_INTERVIEW_WHISPER_MODEL = "small"
```

To check setup, open the device-check page after starting an interview. It
synthesizes a real test sentence, rejects silent/short/malformed WAVs, and
reports speech synthesis and browser playback as separate checks. To force the
canned transcript again (offline demos or CI), set `MOCK_INTERVIEW_DUMMY_STT=true`.

## Real interviewer voice (Kokoro ONNX)

The real TTS path uses the open-source `kokoro-onnx` package and local model
files. It does not call a cloud service and does not require Piper or PyTorch:

```powershell
python -m pip install kokoro-onnx soundfile
```

Download both files from the Kokoro ONNX releases and keep them together:

| File | Approximate size |
|------|------------------|
| `kokoro-v1.0.onnx` | 326 MB |
| `voices-v1.0.bin` | 28 MB |

By default the app looks in
`mock_interview/model_data/kokoro/`. To keep the models outside the repository
(or outside a synced OneDrive folder), point the settings at them:

```powershell
$env:MOCK_INTERVIEW_KOKORO_ONNX_MODEL = "C:\path\to\kokoro-v1.0.onnx"
$env:MOCK_INTERVIEW_KOKORO_ONNX_VOICES = "C:\path\to\voices-v1.0.bin"
$env:MOCK_INTERVIEW_DUMMY_TTS = "false"
```

Useful optional settings:

* `MOCK_INTERVIEW_TTS_BACKEND=kokoro_onnx` forces the ONNX backend. The default
  `auto` prefers the PyTorch `kokoro` package when installed, then ONNX, then
  Piper.
* `MOCK_INTERVIEW_KOKORO_VOICE=af_heart` selects the voice; the interview can
  also store its own `interviewer_voice`.
* `MOCK_INTERVIEW_TTS_SPEED=0.95` controls speaking rate.
* `MOCK_INTERVIEW_TTS_CACHE_ENABLED=true` (default) caches synthesized WAVs in
  `media/tts_cache/`; `MOCK_INTERVIEW_TTS_CACHE_DIR` changes the location.

The first synthesis in a new process is much slower than steady state: loading
the ONNX model takes a few seconds and the first inference is slower than later
ones. The background interview worker warms the engine at startup, and all
synthesis is serialized so the warm-up and a room request cannot race.

The device check separates **speech synthesis** from **browser playback**. It
synthesizes a real probe sentence and only reports the voice as ready if the
result is valid, non-silent PCM. The room follows
`GENERATING_QUESTION → SYNTHESIZING_AUDIO → PLAYING_QUESTION → READY_TO_ANSWER`;
recording stays locked until the browser has actually played the question. If
synthesis, autoplay or playback fails, the failure and a "Retry question audio"
button remain visible instead of silently falling back to text-only.

## Common commands

```
python manage.py migrate
python manage.py seed_dummy_data
python manage.py runserver
python manage.py run_interview_worker        # optional background worker
python manage.py run_mock_interview_benchmark
```

## Running the tests

The app's unit tests use a dedicated settings module (in-memory SQLite,
synchronous pipeline) and pass fully under DUMMY_MODE:

```
python manage.py test mock_interview --settings=mock_interview.tests.settings
```
