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
* `speech/stt.py` returns a canned transcript and never loads Whisper.
* `speech/tts.py` writes a short silent WAV instead of calling Kokoro/Piper.
* `rag/embeddings.py` + `rag/vectorstore.py` use deterministic pseudo-embeddings
  and an in-memory vector store.

Set `MOCK_INTERVIEW_DUMMY_MODE=false` to use the real local models/services.

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
