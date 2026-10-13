# Open-Source AI Mock Interview â€” Architecture and Implementation Study

## Document status

Implementation is in progress. The English local MVP described in the
"Implemented now" section works in the existing Django application. The
production queue and multi-GPU deployment described later are an architecture
target, not a claim that this checkout can already serve 1,000 simultaneous
interviews.

The implementation uses no paid AI API. Qwen, Faster-Whisper, and Piper/Kokoro
run locally. Tamil voice remains disabled until an Indic-TTS service is
deployed and benchmarked.

## 1. Project objective

Build a private, locally hosted mock-interview platform for college students.
The platform should:

1. Accept a student resume in PDF or DOCX format, or allow an interview without
   a resume.
2. Accept a target job title, interview round, difficulty, target skills, job
   description, language, duration, and number of questions.
3. Generate a planned interview from the resume and configuration.
4. Speak each question through a local text-to-speech model.
5. Record the student's spoken answer.
6. Transcribe the answer through a local speech-to-text model.
7. Allow the student to review the transcript without silently replacing the
   original transcript.
8. Ask bounded follow-up questions when appropriate.
9. Evaluate each answer against a rubric created before the answer was given.
10. Generate a grounded performance report and downloadable PDF.
11. Preserve server-side authorization so students cannot access another
    student's resume, recording, transcript, interview, or report.

## 2. Implemented now

The current English MVP includes:

- ERP student-only access using the authenticated `Employee_id`
- Server-side ownership checks for every resume, session, question, answer,
  audio file, report, and PDF
- PDF/DOCX validation, parsing, hashing, and local Qwen resume normalization
- Role, round, difficulty, skills, job description, question-count, duration,
  and voice configuration
- UUID-based interview sessions and a persisted state machine
- Resume-aware or role-aware questions with a rubric stored before the answer
- A deterministic NetworkX information graph built from resume facts, interview
  turns, scoring dimensions, missing concepts, improvements, and speech metrics
- Local Qwen 3 structured JSON through Ollama, with a clearly labelled
  deterministic fallback if the local LLM is unavailable
- Local English question audio through Kokoro, using native Kokoro on Python
  3.10-3.12 or Kokoro ONNX on Python 3.13
- Browser microphone/camera checks and browser audio recording
- Audio signature/size validation and Faster-Whisper transcription
- Separate original and student-reviewed transcripts
- Rubric evaluation, deterministic Python score aggregation, coaching report,
  and private PDF
- A student-visible runtime readiness check
- Migration, authorization-helper, resume, scoring, route, audio, and
  interview-state tests

Current boundaries:

- English is enabled. Tamil and mixed TTS are disabled until Indic-TTS exists.
- Inference is synchronous in this MVP. Do not use the Django development
  server or one Ollama process for a 1,000-student production event.
- Video is previewed but not recorded or stored.
- Resume vector search is not used because a single short resume fits safely in
  bounded context. Add embeddings only for larger document collections.
- Neo4j is not required in the MVP. The information graph is built with
  NetworkX and stored as JSON snapshots in MariaDB for simple deployment.
- ClamAV scanning, retention jobs, background queues, load tests, and faculty
  scoring calibration remain production work.

## 3. Open-source technology policy

The finished module must not require Azure, OpenAI, Gemini, Anthropic, AWS AI,
Google Speech, ElevenLabs, or another paid inference API.

The system can download model weights during administrator-controlled setup.
After the models are downloaded, normal interview inference must work without
an internet connection.

Free software still has infrastructure costs. Running the system locally
requires college-owned CPU, RAM, GPU, disk storage, electricity, backups, and
administration. "Free" here means no per-request model API charge.

Every dependency and model must be recorded in a software/model inventory with:

- Name and exact version
- Source repository
- Model-weight source
- Licence
- Model checksum
- Date downloaded
- Intended use

## 4. Recommended model stack

### 4.1 Large language model

Primary production model:

- Model: `qwen3:8b`
- Development runtime: Ollama
- Production runtime: vLLM with continuous batching and replicated GPU workers
- Licence: Apache 2.0 model weights
- Purpose: final reports and the quality benchmark for rubric-based evaluation

CPU development/live-turn model:

- Model: `qwen3:1.7b`
- Purpose: combine answer evaluation and the next adaptive question into one
  bounded JSON response.
- The live-turn schema deliberately requests short evidence and actions. Longer
  coaching prose is generated only for the final report.
- `qwen3:4b` and `qwen3:8b` are too slow for synchronous turn-taking on the
  tested CPU. They remain useful on GPU or for offline/final work.

Higher-quality deployment:

- Model: `qwen3:14b`
- Use when a suitable GPU and at least 32 GB system RAM are available.
- This should be preferred for final answer evaluation after benchmarking.

Low-resource development:

- Model: `qwen3:4b`
- Use only for development, workflow tests, and demonstrations.
- Do not approve it as the production evaluator until faculty reviewers confirm
  acceptable scoring agreement.

Why Qwen 3:

- It is locally runnable through Ollama.
- The same weights can be served at higher throughput through vLLM.
- Its released open-weight models use the Apache 2.0 licence.
- It is multilingual.
- Ollama can constrain output with a JSON Schema.
- It supports a faster non-thinking workflow and a deeper reasoning workflow.

Do not rely on a model name alone. The college must benchmark `8b` and `14b`
using anonymized student answers before selecting the production model.

The Planner, Interviewer, Evaluator, Feedback Writer, and Report Writer are
logical roles controlled by a Django state machine. They are not autonomous
agents and do not each require a separately loaded model. Start with one Qwen 3
model family. Add a second evaluator model only when a faculty-reviewed
benchmark proves that its scoring agreement is materially better.

If DeepSeek is benchmarked, prefer a Qwen-derived distillation such as
DeepSeek-R1-Distill-Qwen over a Llama-derived checkpoint when strict permissive
open-source model lineage is required.

### 4.2 Speech-to-text

Primary library:

- Library: `faster-whisper`
- Licence: MIT
- Engine: CTranslate2

Recommended models:

- GPU production: multilingual Whisper `large-v3-turbo`
- CPU production fallback: multilingual Whisper `medium`, INT8
- Low-resource development: multilingual Whisper `small`, INT8

Use multilingual models rather than `.en` models because the application
supports English, Tamil, and mixed speech.

Recommended inference settings:

| Setting | GPU | CPU |
|---|---|---|
| Device | `cuda` | `cpu` |
| Compute type | `float16` or `int8_float16` | `int8` |
| VAD | Enabled | Enabled |
| Word timestamps | Enabled | Enabled |
| Beam size | Start with 5 and benchmark | Start with 3 and benchmark |
| Temperature | `0` | `0` |
| Condition on previous text | Benchmark per-answer; avoid cross-answer leakage | Same |

The student selects English, Tamil, or Tamil-English mixed during setup. Do not
guess a student's language from their profile.

For technical interviews, create an STT hot-word/context list from:

- Resume skills and project names
- Target job title
- Selected technologies
- Common programming languages, libraries, databases, and abbreviations

The original STT transcript must be retained. A corrected or student-edited
transcript must be stored separately.

### 4.3 Voice activity detection

- Model/library: Silero VAD
- Purpose: remove leading/trailing silence, detect when an answer has started,
  calculate pause metrics, and reduce unnecessary transcription work

VAD must not automatically end an answer after one short pause. Start with:

- Minimum speech: 250 ms
- End-of-answer silence suggestion: 1.2â€“1.8 seconds
- Maximum answer duration: controlled by the interview configuration

The browser must still provide an explicit Stop Answer button.

### 4.4 Text-to-speech

English questions:

- Preferred production model: Kokoro-82M on Python 3.10-3.12
- Python 3.13 runtime: Kokoro v1.0 through `kokoro-onnx`
- Compatible local fallback: Piper
- Licences: Kokoro model Apache 2.0; `kokoro-onnx` MIT; current Piper
  engine GPL-3.0
- Use: locally generated English interviewer voice

Kokoro remains the preferred natural voice after benchmarking. The standard
`kokoro` package requires Python below 3.13, so this Windows checkout uses its
ONNX Runtime implementation. The `TTS_BACKEND=auto` setting prefers a ready
Kokoro backend and retains Piper as fallback without using browser
`speechSynthesis`.

Tamil questions:

- Model: AI4Bharat Indic-TTS Tamil
- Licence: MIT
- Use: locally generated Tamil interviewer voice

Tamil-English mixed questions:

1. Ask the LLM to return `display_text` plus ordered `speech_segments`.
2. Each segment must declare `en` or `ta`.
3. English segments are synthesized with Kokoro.
4. Tamil segments are synthesized with Indic-TTS.
5. FFmpeg joins the segments with a short pause.

Example structured result:

```json
{
  "display_text": "Explain inheritance and give à®’à®°à¯ practical example.",
  "speech_segments": [
    {"language": "en", "text": "Explain inheritance and give"},
    {"language": "ta", "text": "à®’à®°à¯"},
    {"language": "en", "text": "practical example."}
  ]
}
```

Do not send Romanized Tamil directly to a Tamil TTS model. If the product later
accepts Tanglish text, use AI4Bharat IndicXlit to transliterate the Tamil parts
to Tamil script and allow the user to verify the result.

TTS output rules:

- WAV is the internal master format.
- Browser delivery can use Opus or MP3 generated by FFmpeg.
- Normalize loudness consistently.
- Add 300â€“500 ms silence before the question.
- Cache by the hash of text, language, model, voice, speed, and model version.
- Repeat Question replays the stored audio; it must not regenerate the question.

### 4.5 Resume processing

Use deterministic parsing before involving the LLM:

- PDF: `pypdf`
- DOCX: `python-docx`
- File inspection: `python-magic` or an equivalent content-signature check
- Malware scanning: ClamAV in production

The LLM may normalize extracted text into structured resume fields, but it must
not be responsible for opening arbitrary files.

Resume text is untrusted input. Statements such as "ignore previous
instructions" inside a resume must be treated as resume content, never as
system instructions.

### 4.6 Background processing

- Worker: Celery, New BSD licence
- Durable production broker: RabbitMQ quorum queues
- Cache, short-lived state, rate limits, and channel layer: Valkey
- Celery result records: database or a separately managed Valkey namespace

Valkey is preferred over newer Redis distributions because this design requires
an unambiguously open-source server. RabbitMQ is preferred for durable
production jobs so that the cache is not also the only task-delivery system.

Run Celery, RabbitMQ, and Valkey through Linux, Docker, or WSL2 during
development.
Production workers should run on Linux.

### 4.7 Supporting tools

| Requirement | Tool |
|---|---|
| Audio conversion and concatenation | FFmpeg |
| Development LLM runtime | Ollama |
| Production LLM runtime | vLLM |
| API validation | Django REST Framework + Pydantic/JSON Schema |
| PDF generation | ReportLab already present, or WeasyPrint after review |
| Database | Existing Django database initially; PostgreSQL is preferred for a new fully open-source deployment |
| Development media | Private local media directory |
| Pilot media storage | Encrypted private shared filesystem |
| Large production object storage | Ceph Object Gateway or an institution-managed S3-compatible service |
| Shared knowledge vector database | Qdrant, only if shared RAG is approved |

## 5. Hardware profiles

Model selection must be controlled through environment settings, not hard-coded
in business logic.

### Profile A: development laptop

- 16 GB RAM
- CPU inference
- `qwen3:4b` quantized
- Faster Whisper `small` INT8
- Kokoro for English TTS
- Short interviews with no concurrency

This profile demonstrates functionality but is not a production scoring
standard.

### Profile B: recommended college pilot

- 32 GB RAM
- NVIDIA GPU with approximately 12â€“16 GB VRAM
- `qwen3:8b` or benchmarked `qwen3:14b` quantized
- Faster Whisper `large-v3-turbo`
- Kokoro and Indic-TTS
- Separate web and worker processes
- RabbitMQ task broker and Valkey cache

### Profile C: multi-student production

- Linux inference server
- 64 GB or more RAM
- 24 GB or more GPU VRAM, or multiple inference workers
- Benchmark `qwen3:14b` and `qwen3:30b-a3b`
- Serve production LLM replicas through vLLM rather than Ollama
- Dedicated STT worker and LLM worker queues
- Concurrency limits and admission control
- Private object storage, monitoring, backup, and retention jobs

Do not promise a concurrency number before load testing on the exact hardware.

## 6. End-to-end system architecture

```text
Student browsers
       |
       v
HAProxy or Nginx ingress
       |
       +-------------------+
       |                   |
       v                   v
Django ASGI API      WebSocket/status pods
       |                   |
       +---------+---------+
                 |
        Django orchestrator
                 |
       +---------+-------------------+
       |         |                   |
       v         v                   v
   Database    Valkey             RabbitMQ
   metadata    cache/state        durable tasks
                                     |
                 +-------------------+-------------------+
                 |                   |                   |
                 v                   v                   v
          STT GPU workers   interactive vLLM     batch workers
          Faster Whisper       Qwen 3          evaluation/report
                 |                   |                   |
                 +-------------------+-------------------+
                                     |
                           Kokoro / Indic-TTS
                                     |
                           private media storage
```

The AI model never queries unrestricted ERP data. Django retrieves only the
authenticated student's permitted resume and interview records and passes the
minimum necessary data to local inference services.

## 7. Interview lifecycle

Use an explicit state machine. Never infer state from which page the student is
viewing.

```text
draft
  â†’ resume_processing
  â†’ planning
  â†’ ready
  â†’ in_progress
      â†’ question_speaking
      â†’ answer_recording
      â†’ transcribing
      â†’ transcript_review
      â†’ answer_submitted
      â†’ next_question OR completing
  â†’ evaluating
  â†’ report_ready
```

Recoverable failure states:

- `resume_failed`
- `planning_failed`
- `transcription_failed`
- `tts_failed`
- `evaluation_failed`
- `report_failed`
- `expired`
- `cancelled`

Every transition must be validated on the server. A browser must not be able to
move a session directly from `draft` to `report_ready`.

## 8. Interview planning behavior

At session creation, generate an interview blueprint containing:

- Interview objective
- Question categories
- Difficulty distribution
- Resume-derived topics
- Job-description-derived topics
- Round-specific competencies
- Time allocation
- Maximum number of follow-ups

Generate and save the first question and its rubric before the interview starts.
Later questions may be generated just in time, but each question and rubric
must be committed before the student begins answering it.

Question sources should be marked as:

- `resume`
- `job_description`
- `target_skill`
- `question_bank`
- `adaptive_follow_up`
- `general_round`

The system must be able to explain why a question was selected.

## 9. LLM operating modes

### 9.1 Resume normalization

- Model: Qwen 3
- Thinking: off
- Temperature: 0
- Output: strict `ResumeProfile` JSON Schema
- Input: sanitized extracted text only

### 9.2 Interview blueprint

- Thinking: off
- Temperature: 0.3
- Output: strict `InterviewBlueprint` schema
- Validate counts, allowed round types, and difficulty values in Python

### 9.3 Question generation

- Thinking: off
- Temperature: 0.3â€“0.5
- Output: question, reason, category, expected duration, rubric, expected
  concepts, and TTS segments
- Maximum one question per response

### 9.4 Adaptive follow-up

- Thinking: off
- Temperature: 0.2
- Small output-token limit
- Follow-up is allowed only when the preceding transcript contains enough
  evidence and the session follow-up limit has not been reached

### 9.5 Answer evaluation

- Thinking: on if supported reliably by the selected Ollama/model version
- Temperature: 0â€“0.1
- Output: strict `AnswerEvaluation` schema
- Input: question, precommitted rubric, expected concepts, original transcript,
  reviewed transcript, and deterministic speech metrics
- Every criticism must cite transcript evidence or identify an absent expected
  concept

### 9.6 Report wording

- Thinking: off
- Temperature: 0.2
- Input: validated per-answer evaluations and Python-calculated totals
- The model writes explanations but cannot change scores

Ollama JSON Schema enforcement guarantees structure, not factual correctness.
All values must still be validated and bounded in Python.

## 10. Evaluation and scoring design

Do not ask the LLM for one unexplained overall score.

Recommended technical-round dimensions:

| Dimension | Weight |
|---|---:|
| Technical correctness | 40% |
| Completeness | 15% |
| Relevance | 15% |
| Explanation structure | 10% |
| Practical example/application | 10% |
| Communication delivery | 10% |

Recommended HR/behavioural dimensions:

| Dimension | Weight |
|---|---:|
| Relevance to question | 20% |
| STAR structure | 25% |
| Evidence/specificity | 20% |
| Reflection and learning | 15% |
| Communication delivery | 20% |

Each dimension is returned on a bounded 0â€“10 scale. Python:

1. Rejects values outside the range.
2. Applies the stored round-specific weights.
3. Calculates question totals.
4. Aggregates interview totals.
5. Rounds only for presentation.

The report should display:

- Overall score
- Technical/content score
- Communication-delivery score
- Question-by-question rubric scores
- Transcript evidence
- Strengths
- Missing concepts
- Improved answer outline
- Recommended practice topics
- Comparison with the student's previous attempts, when available

Do not evaluate attractiveness, emotion, eye shape, skin tone, disability,
personality, honesty, or employability from camera images.

Rename the prototype "Confidence" score to "Communication Delivery" unless a
clear, reviewed scoring rubric is defined.

### 10.1 Evaluation benchmark and calibration

The code does not train or fine-tune an LLM. It uses an existing local model and
checks whether the AI evaluator agrees with expert or faculty scoring.

Do not claim:

```text
Training score = 99%
Testing score = 99%
Therefore the evaluator is accurate.
```

For interview scoring, the safer production method is evaluator calibration:

```text
Expert-scored benchmark answers
  -> current AI evaluator
  -> predicted scores
  -> compare with expert scores
  -> identify over-scoring and under-scoring
  -> adjust rubric/prompt/model
  -> test again on unseen holdout answers
```

Implemented benchmark files:

```text
mock_interview/evaluation_benchmark/
  metrics.py
  runner.py
  datasets/example_evaluation_cases.jsonl
mock_interview/management/commands/run_mock_interview_benchmark.py
```

Dataset format is JSONL, one case per line:

```json
{
  "id": "technical-001",
  "question": "Can you describe a time when you used Python or SQL?",
  "transcript": "Student-confirmed answer text...",
  "rubric": {
    "technical_correctness": 40,
    "completeness": 20,
    "relevance": 15,
    "structure": 15,
    "practical_example": 10
  },
  "expected_concepts": ["Python", "SQL", "measurable outcome"],
  "expert_total_score": 84,
  "expert_dimension_scores": {
    "technical_correctness": 8.5,
    "completeness": 8,
    "relevance": 9,
    "structure": 8,
    "practical_example": 8.5
  },
  "speech_metrics": {
    "word_count": 66,
    "words_per_minute": 132,
    "filler_count": 0,
    "pause_seconds": 1
  }
}
```

Run the benchmark from the workspace root:

```powershell
python RIT\ramco_academic_system\manage.py run_mock_interview_benchmark RIT\ramco_academic_system\mock_interview\evaluation_benchmark\datasets\example_evaluation_cases.jsonl
```

The evaluator also applies deterministic calibration caps after the LLM returns
scores. Very short, vague, or generic answers without expected concepts,
implementation detail, or outcome evidence cannot receive high rubric scores
only because the model is generous. This cap is a guardrail, not a replacement
for faculty calibration.

Useful output fields:

- `mean_absolute_error`: average AI-vs-expert score difference
- `within_tolerance_accuracy`: percentage of cases within the allowed error
- `dimension_metrics`: agreement per rubric dimension
- `over_scored_cases`: answers where AI scored too high
- `under_scored_cases`: answers where AI scored too low
- `failed_case_ids`: cases where evaluator execution failed

Recommended acceptance target before production:

- At least 100 anonymized expert-scored answers
- Separate calibration and holdout datasets
- Overall score mean absolute error below 8 to 10 points on the 0-100 scale
- Dimension score mean absolute error below 1 point on the 0-10 scale
- Manual review of all over-scored and under-scored cases
- Faculty approval of scoring language and improvement advice

## 11. Deterministic speech metrics

Calculate these without an LLM:

- Answer duration
- Total recognized words
- Words per minute
- Initial response delay
- Number and duration of pauses
- Pause ratio
- Filler-word count and ratio
- Repeated phrase count
- Very short or incomplete answer indicator

Speech metrics are coaching aids, not medical or psychological assessments.
Students must not be penalized for accent alone.

## 11.1 Information graph method

The MVP uses `networkx` for the information graph layer.

Why `networkx`:

- It is already a Python library and does not require a separate database.
- It is free and open source.
- It is enough for resume/project/skill/question/evaluation relationships.
- It can export nodes and edges as JSON for MariaDB storage.
- The same exported graph can later be migrated to Neo4j if the project needs
  advanced graph queries or visual graph exploration.

The graph is deterministic. It does not train the LLM and it does not replace
the rubric score. It organizes known facts so the interviewer and report can
reason over relationships.

### Resume graph

Built after resume parsing and normalization.

Pipeline:

```text
ResumeDocument
  -> structured_profile
  -> NetworkX DiGraph
  -> nodes: student, resume, skills, projects, education, experience
  -> edges: uploaded, mentions_skill, mentions_project, uses_skill
  -> ResumeDocument.information_graph JSON
```

Example relationship:

```text
Project: Feedback classifier
  -> uses_skill -> Python
  -> uses_skill -> SQL
```

This helps the AI ask project-linked questions instead of only keyword
questions.

### Interview graph

Built during final report generation.

Pipeline:

```text
InterviewSession
  -> questions
  -> confirmed answers
  -> evaluations
  -> speech metrics
  -> NetworkX DiGraph
  -> InterviewReport.information_graph JSON
```

The report graph stores:

- Central concepts
- Strongest dimensions
- Weakest dimensions
- Priority improvements
- Missing concepts
- Speech observations
- Resume-based follow-up focus

The LLM sees only a compact graph summary in prompts. The full graph snapshot is
stored for audit and student-facing report insights.

## 12. Data model design

### ResumeDocument

- `id`
- `student`
- `file`
- `original_filename`
- `mime_type`
- `sha256`
- `file_size`
- `status`
- `extracted_text`
- `structured_profile`
- `information_graph`
- `parser_version`
- `created_at`
- `retention_until`

### InterviewSession

- `id`
- `public_id` UUID
- `student`
- `resume`
- `interview_type`
- `job_title`
- `job_description`
- `interview_round`
- `difficulty`
- `target_skills`
- `language_mode`
- `question_count`
- `duration_minutes`
- `follow_up_limit`
- `status`
- `prompt_version`
- `scoring_version`
- `llm_model`
- `stt_model`
- `tts_model`
- `consent_version`
- `consented_at`
- `started_at`
- `completed_at`
- `created_at`
- `last_error_code`

### InterviewQuestion

- `session`
- `sequence_number`
- `question_text`
- `display_text`
- `speech_segments`
- `source`
- `selection_reason`
- `question_type`
- `difficulty`
- `rubric`
- `expected_concepts`
- `is_follow_up`
- `parent_question`
- `model_name`
- `prompt_version`
- `audio_file`
- `audio_sha256`
- `created_at`

### StudentAnswer

- `question`
- `audio_file`
- `audio_sha256`
- `duration_seconds`
- `original_transcript`
- `reviewed_transcript`
- `transcript_changed`
- `detected_language`
- `stt_confidence`
- `word_timestamps`
- `speech_metrics`
- `stt_model`
- `recorded_at`
- `submitted_at`

### AnswerEvaluation

- `answer`
- `dimension_scores`
- `total_score`
- `evidence`
- `strengths`
- `missing_concepts`
- `improvement_actions`
- `improved_answer_outline`
- `model_name`
- `prompt_version`
- `rubric_version`
- `raw_validated_output`
- `created_at`

### InterviewReport

- `session`
- `overall_score`
- `content_score`
- `delivery_score`
- `aggregate_scores`
- `summary`
- `strengths`
- `improvement_plan`
- `recommended_topics`
- `information_graph`
- `pdf_file`
- `generated_at`

### ModelRunAudit

- `session`
- `operation`
- `provider`
- `model`
- `model_digest`
- `prompt_version`
- `input_hash`
- `output_hash`
- `duration_ms`
- `status`
- `error_code`
- `created_at`

Do not store hidden model reasoning. Store validated outputs, hashes, model
metadata, and the evidence used for scores.

## 13. API design

All endpoints require authentication. Object endpoints require
`object.student == request.user` unless the requester has an explicitly defined
placement-administrator permission.

### Resume endpoints

```text
POST   /mock-interview/api/resumes/
GET    /mock-interview/api/resumes/
GET    /mock-interview/api/resumes/{public_id}/
DELETE /mock-interview/api/resumes/{public_id}/
```

### Session endpoints

```text
POST  /mock-interview/api/sessions/
GET   /mock-interview/api/sessions/
GET   /mock-interview/api/sessions/{public_id}/
PATCH /mock-interview/api/sessions/{public_id}/
POST  /mock-interview/api/sessions/{public_id}/consent/
POST  /mock-interview/api/sessions/{public_id}/start/
POST  /mock-interview/api/sessions/{public_id}/end/
GET   /mock-interview/api/sessions/{public_id}/status/
```

### Interview endpoints

```text
GET  /mock-interview/api/sessions/{public_id}/current-question/
POST /mock-interview/api/questions/{public_id}/repeat/
POST /mock-interview/api/questions/{public_id}/skip/
POST /mock-interview/api/questions/{public_id}/answers/
GET  /mock-interview/api/answers/{public_id}/transcription-status/
PATCH /mock-interview/api/answers/{public_id}/transcript/
POST /mock-interview/api/answers/{public_id}/submit/
```

### Report/media endpoints

```text
GET /mock-interview/api/sessions/{public_id}/report/
GET /mock-interview/api/sessions/{public_id}/report.pdf
GET /mock-interview/api/questions/{public_id}/audio/
GET /mock-interview/api/answers/{public_id}/audio/
```

Use idempotency keys for session creation, answer upload, answer submission, and
end-interview operations.

## 14. Browser workflow

### Resume page

1. Choose or drop a PDF/DOCX file.
2. Validate size and extension in the browser for immediate feedback.
3. Upload to Django for authoritative validation.
4. Poll parsing status.
5. Show extracted skills/projects for student confirmation.
6. Allow Skip Resume.

### Setup page

Collect:

- Resume-based, role-based, or mixed interview
- Job title
- Job description
- Interview round
- Difficulty
- Target skills
- Number of questions
- Duration
- English, Tamil, or mixed language
- Interviewer voice

POST the configuration to create a real draft session.

### Device page

- Request camera/microphone permission only after a user action.
- List actual input devices.
- Record and play a local microphone sample.
- Play a sample generated by the selected local TTS model.
- Check backend, STT worker, TTS worker, and Ollama readiness.
- Continue only when required checks pass.

### Consent page

- Use the real session UUID.
- Store consent version and timestamp.
- Explain recording, transcript, evaluation, retention, and deletion.
- Start through a POST request protected by CSRF.

### Interview room

Browser state:

```text
loading_question
â†’ playing_question
â†’ ready_to_answer
â†’ recording
â†’ uploading
â†’ transcribing
â†’ transcript_review
â†’ submitting
â†’ loading_next_question
```

Rules:

- Do not record while question audio is playing.
- Disable duplicate submissions.
- Keep audio locally until the server confirms upload.
- Show upload/transcription errors with Retry.
- Repeat replays identical stored audio.
- Skip records a reason/state; it does not delete the question.
- Refresh restores state from the server.
- Stop all camera/microphone tracks when leaving.

### Processing page

Poll real task state rather than redirecting after a fixed timeout.

Display stages:

- Upload verified
- Transcription complete
- Answers being evaluated
- Scores being aggregated
- Report being generated
- Report ready

### Report page

Render database values only. Provide:

- Score summary
- Dimension chart
- Question accordion
- Original/reviewed transcript labels
- Evidence-based feedback
- Improved answer outline
- Practice plan
- Authorized recording playback
- Authorized PDF download
- Retake with same configuration

## 15. Proposed application tree

```text
mock_interview/
â”œâ”€â”€ README.md
â”œâ”€â”€ __init__.py
â”œâ”€â”€ admin.py
â”œâ”€â”€ apps.py
â”œâ”€â”€ urls.py
â”œâ”€â”€ api/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ urls.py
â”‚   â”œâ”€â”€ permissions.py
â”‚   â”œâ”€â”€ serializers.py
â”‚   â”œâ”€â”€ resume_views.py
â”‚   â”œâ”€â”€ session_views.py
â”‚   â”œâ”€â”€ interview_views.py
â”‚   â””â”€â”€ report_views.py
â”œâ”€â”€ ai/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ ollama_client.py
â”‚   â”œâ”€â”€ schemas.py
â”‚   â”œâ”€â”€ prompt_loader.py
â”‚   â”œâ”€â”€ resume_normalizer.py
â”‚   â”œâ”€â”€ interview_planner.py
â”‚   â”œâ”€â”€ question_generator.py
â”‚   â”œâ”€â”€ evaluator.py
â”‚   â”œâ”€â”€ report_writer.py
â”‚   â””â”€â”€ prompts/
â”‚       â”œâ”€â”€ resume_profile_v1.txt
â”‚       â”œâ”€â”€ interview_plan_v1.txt
â”‚       â”œâ”€â”€ question_v1.txt
â”‚       â”œâ”€â”€ follow_up_v1.txt
â”‚       â”œâ”€â”€ technical_evaluation_v1.txt
â”‚       â”œâ”€â”€ behavioural_evaluation_v1.txt
â”‚       â””â”€â”€ report_v1.txt
â”œâ”€â”€ models/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ resume.py
â”‚   â”œâ”€â”€ session.py
â”‚   â”œâ”€â”€ question.py
â”‚   â”œâ”€â”€ answer.py
â”‚   â”œâ”€â”€ evaluation.py
â”‚   â”œâ”€â”€ report.py
â”‚   â””â”€â”€ audit.py
â”œâ”€â”€ resume/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ validators.py
â”‚   â”œâ”€â”€ pdf_parser.py
â”‚   â”œâ”€â”€ docx_parser.py
â”‚   â”œâ”€â”€ sanitizer.py
â”‚   â””â”€â”€ profile_builder.py
â”œâ”€â”€ speech/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ audio.py
â”‚   â”œâ”€â”€ vad.py
â”‚   â”œâ”€â”€ stt.py
â”‚   â”œâ”€â”€ stt_models.py
â”‚   â”œâ”€â”€ tts.py
â”‚   â”œâ”€â”€ kokoro_tts.py
â”‚   â”œâ”€â”€ indic_tts.py
â”‚   â”œâ”€â”€ transliteration.py
â”‚   â””â”€â”€ metrics.py
â”œâ”€â”€ services/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ session_service.py
â”‚   â”œâ”€â”€ interview_state.py
â”‚   â”œâ”€â”€ question_service.py
â”‚   â”œâ”€â”€ answer_service.py
â”‚   â”œâ”€â”€ scoring.py
â”‚   â”œâ”€â”€ report_service.py
â”‚   â”œâ”€â”€ media_service.py
â”‚   â””â”€â”€ retention_service.py
â”œâ”€â”€ tasks/
â”‚   â”œâ”€â”€ __init__.py
â”‚   â”œâ”€â”€ resume_tasks.py
â”‚   â”œâ”€â”€ speech_tasks.py
â”‚   â”œâ”€â”€ interview_tasks.py
â”‚   â”œâ”€â”€ evaluation_tasks.py
â”‚   â”œâ”€â”€ report_tasks.py
â”‚   â””â”€â”€ retention_tasks.py
â”œâ”€â”€ migrations/
â”œâ”€â”€ management/
â”‚   â””â”€â”€ commands/
â”‚       â”œâ”€â”€ check_mock_interview_ai.py
â”‚       â”œâ”€â”€ download_mock_interview_models.py
â”‚       â””â”€â”€ benchmark_mock_interview_models.py
â”œâ”€â”€ templates/
â”‚   â””â”€â”€ mock_interview/
â”‚       â”œâ”€â”€ dashboard.html
â”‚       â”œâ”€â”€ setup.html
â”‚       â”œâ”€â”€ device_check.html
â”‚       â”œâ”€â”€ instructions.html
â”‚       â”œâ”€â”€ room.html
â”‚       â”œâ”€â”€ processing.html
â”‚       â”œâ”€â”€ report.html
â”‚       â””â”€â”€ includes/
â”œâ”€â”€ static/
â”‚   â””â”€â”€ mock_interview/
â”‚       â”œâ”€â”€ css/
â”‚       â”‚   â””â”€â”€ style.css
â”‚       â””â”€â”€ js/
â”‚           â”œâ”€â”€ api.js
â”‚           â”œâ”€â”€ resume.js
â”‚           â”œâ”€â”€ setup.js
â”‚           â”œâ”€â”€ device_check.js
â”‚           â”œâ”€â”€ recorder.js
â”‚           â”œâ”€â”€ interview_room.js
â”‚           â”œâ”€â”€ processing.js
â”‚           â””â”€â”€ report.js
â””â”€â”€ tests/
    â”œâ”€â”€ __init__.py
    â”œâ”€â”€ factories.py
    â”œâ”€â”€ audio_fixtures/
    â”œâ”€â”€ test_permissions.py
    â”œâ”€â”€ test_resume_validation.py
    â”œâ”€â”€ test_state_machine.py
    â”œâ”€â”€ test_stt.py
    â”œâ”€â”€ test_tts.py
    â”œâ”€â”€ test_question_generation.py
    â”œâ”€â”€ test_evaluation.py
    â”œâ”€â”€ test_scoring.py
    â”œâ”€â”€ test_api_idempotency.py
    â””â”€â”€ test_full_interview.py
```

## 16. File-by-file responsibilities

### `rit_academic_system/settings.py`

- Environment-only model and runtime configuration
- Ollama URL and model
- STT/TTS model paths
- CPU/GPU inference mode
- timeouts and concurrency
- upload and audio limits
- private media root
- Celery and Valkey settings
- retention duration

No secret may be committed to this file.

### `requirements.txt`

Add dependencies only after licence and Python-version checks. Pin versions so
the college can reproduce the environment.

Expected additions include:

- `faster-whisper`
- Silero VAD dependency
- Kokoro inference package
- AI4Bharat TTS dependencies
- `python-docx`
- file-signature validation
- `celery`
- Valkey-compatible Python client

FFmpeg, Ollama, Valkey, and ClamAV are system services/binaries and should be
documented separately from Python packages.

### `models/`

Split domain models by responsibility and export them through
`models/__init__.py` so Django discovers them consistently.

### `api/`

Validate HTTP data, apply permissions, call services, and return responses.
Views must not contain model prompts or inference implementation.

### `services/`

Own business rules, transactions, state transitions, idempotency, scoring, and
media authorization.

### `ai/`

Own prompts, schemas, Ollama communication, response parsing, and validation.
It must not directly grant database access.

### `speech/`

Own audio normalization, VAD, transcription, synthesis, transliteration, and
deterministic delivery metrics.

### `tasks/`

Contain short Celery task wrappers. Business rules remain in services so they
can also be tested synchronously.

### Templates and static JavaScript

Templates render the initial page and safe configuration. JavaScript manages
browser devices and calls authenticated APIs. No score is accepted from the
browser.

## 17. Security and privacy requirements

- Enforce student role and object ownership on the server.
- Use UUID public identifiers.
- Use CSRF protection for state-changing requests.
- Keep media private; never expose predictable `/media/answers/...` URLs.
- Validate MIME type, extension, size, and file signature.
- Scan uploaded resumes.
- Escape resume and model output before rendering.
- Treat resume/job descriptions/transcripts as untrusted prompt data.
- Rate-limit session creation and inference operations.
- Log authorization failures without exposing private data.
- Encrypt backups and restrict administrator access.
- Define retention periods for resumes, audio, video, transcripts, and reports.
- Provide student deletion where policy permits.
- Do not send student data to external telemetry services.
- Do not record camera video unless it has an approved educational purpose and
  explicit consent.

## 18. Reliability requirements

- Each task is idempotent.
- Save audio before scheduling transcription.
- Save a question and rubric before playing the question.
- Use database transactions for state changes.
- Apply timeouts to Ollama, STT, and TTS.
- Retry transient failures with bounded exponential backoff.
- Do not retry invalid model output forever.
- Store safe error codes for student-facing recovery.
- Use health checks for Ollama, worker queues, model files, FFmpeg, and disk.
- Limit simultaneous LLM/STT jobs to measured hardware capacity.

## 19. Testing and evaluation plan

### Authorization tests

- Student A cannot access Student B's session.
- Student A cannot download Student B's resume, audio, video, transcript, or
  report.
- A forged session UUID is rejected.
- An unauthenticated request is rejected.
- UI restrictions are never the only access control.

### Resume tests

- Valid PDF and DOCX accepted.
- Renamed executable rejected.
- Oversized file rejected.
- Password-protected or malformed document handled safely.
- Prompt-injection text remains data.
- Extracted resume claims are traceable to source text.

### Speech tests

Build consented fixtures for:

- Indian English
- Tamil
- Tamil-English code switching
- Quiet and noisy rooms
- Slow, normal, and fast speech
- Common programming terms
- Different microphones

Measure word error rate rather than judging samples by impression.

### TTS tests

- English and Tamil output generated locally.
- Mixed segments play in order.
- Repeat uses the same cached file.
- Question text and spoken audio match.
- Audio level is consistent.
- Unsupported text fails safely.

### LLM tests

- Output validates against its JSON Schema.
- Question count and difficulty remain within configuration.
- Resume questions do not invent experience.
- Follow-up questions relate to transcript evidence.
- Evaluation includes evidence.
- Scores remain bounded.
- Malicious resume/job-description instructions cannot override the system
  prompt.

### Scoring tests

- Weighted totals are calculated in Python.
- Same stored dimension values always produce the same total.
- Missing evaluation cannot be treated as zero without being labeled.
- Rounding is presentation-only.
- Report text cannot change stored scores.

### Faculty validation

Create an anonymized benchmark set independently scored by at least two faculty
reviewers. Compare AI and human scores by dimension. Establish acceptable
agreement before using scores for student guidance.

AI scores are coaching feedback and must not automatically determine placement,
eligibility, grades, discipline, or employment.

## 20. Implementation phases

### Phase 0 â€” security prerequisite

- Rotate exposed keys.
- Move configuration to environment variables.
- Restore server-side student ownership checks.
- Remove hard-coded session ID `1`.
- Decide retention and consent policy.

### Phase 1 â€” domain and APIs

- Add models and migrations.
- Implement permissions.
- Add session state machine.
- Add resume/session APIs.
- Replace fake navigation with real UUID sessions.

### Phase 2 â€” resume pipeline

- Validate and scan uploads.
- Parse PDF/DOCX.
- Normalize a structured profile through Qwen.
- Let the student confirm extracted details.

### Phase 3 â€” question planning and TTS

- Create versioned prompts and schemas.
- Generate blueprint, question, and precommitted rubric.
- Generate/cache local question audio.
- Replace browser speech synthesis.

### Phase 4 â€” recording and STT

- Implement robust MediaRecorder behavior.
- Upload answer audio.
- Run VAD and Faster Whisper.
- Display original and reviewed transcripts.

### Phase 5 â€” complete interview loop

- Submit answers idempotently.
- Generate bounded follow-ups.
- Restore state after refresh.
- Support repeat, skip, timeout, and end interview.

### Phase 6 â€” evaluation and report

- Evaluate each answer in a background queue.
- Validate evidence and dimension scores.
- Aggregate scores in Python.
- Generate report and PDF.

### Phase 7 â€” quality and deployment

- Benchmark models and hardware.
- Faculty-review scoring agreement.
- Run security and load tests.
- Configure monitoring, backup, retention, and cleanup.
- Pilot with a small consenting student group.

Do not begin a later phase until the acceptance criteria of the earlier phase
pass.

## 21. Definition of done

The feature is complete only when:

- No page uses hard-coded interview data.
- All model inference runs locally.
- A real resume or role-only configuration produces a real interview.
- Questions and audio are persisted.
- Spoken answers are recorded and transcribed.
- The original transcript is preserved.
- Evaluations use precommitted rubrics and transcript evidence.
- Python calculates all final scores.
- Reports and PDFs contain real data.
- Every private object is protected by server-side authorization.
- Failure recovery, idempotency, retention, and deletion are tested.
- English and Tamil benchmarks meet an approved quality threshold.
- Faculty reviewers approve the feedback behavior.
- A reproducible local deployment guide and model manifest exist.

## 22. References

- Faster Whisper: <https://github.com/SYSTRAN/faster-whisper>
- Ollama structured outputs:
  <https://docs.ollama.com/capabilities/structured-outputs>
- Qwen 3: <https://github.com/QwenLM/Qwen3>
- AI4Bharat Indic-TTS: <https://github.com/AI4Bharat/Indic-TTS>
- AI4Bharat IndicXlit: <https://github.com/AI4Bharat/IndicXlit>
- Celery: <https://github.com/celery/celery>
- Valkey: <https://github.com/valkey-io/valkey>

## 23. Exact local setup and run sequence

Run these commands from the directory containing `manage.py`.

### Step 1 - choose the Python/TTS mode

- Recommended production Python: 3.11 or 3.12 with Kokoro.
- This Windows checkout uses Python 3.13 with `kokoro-onnx`.
- Piper remains installed as the local fallback.

Install the pinned packages:

```powershell
python -m pip install -r requirements.txt
```

### Step 2 - prepare the LLM

Start Ollama, then download the configured model:

```powershell
ollama serve
ollama pull qwen3:1.7b
ollama pull qwen3:8b
ollama list
```

Development mode uses Ollama. Production mode uses the same model family behind
vLLM's OpenAI-compatible endpoint and sets
`MOCK_INTERVIEW_OLLAMA_BASE_URL=http://llm-gateway:8000/v1`.

### Step 3 - prepare STT

Faster-Whisper downloads its selected model on first load. Pre-warm it before a
student event:

```powershell
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('STT ready')"
```

The quality-first default is `large-v3-turbo`. CPU mode is suitable for local
testing but is slow. Production should run and benchmark it on CUDA workers.

### Step 4 - prepare local TTS

Python 3.13/Kokoro ONNX mode:

```powershell
New-Item -ItemType Directory -Force mock_interview/model_data/kokoro
Invoke-WebRequest -Uri "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" -OutFile "mock_interview/model_data/kokoro/kokoro-v1.0.onnx"
Invoke-WebRequest -Uri "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" -OutFile "mock_interview/model_data/kokoro/voices-v1.0.bin"
```

The downloaded model data is ignored by Git. The application uses the
`af_heart` voice and automatically selects `kokoro-onnx` when both files are
present.

Piper fallback:

```powershell
New-Item -ItemType Directory -Force mock_interview/model_data/piper
python -m piper.download_voices --download-dir mock_interview/model_data/piper en_US-lessac-medium
```

The voice weights are ignored by Git. The default model path is:

```text
mock_interview/model_data/piper/en_US-lessac-medium.onnx
```

Python 3.11/3.12 installs the native Kokoro package from `requirements.txt`.
Setting `MOCK_INTERVIEW_TTS_BACKEND=kokoro` selects native Kokoro when
available and transparently selects Kokoro ONNX on Python 3.13.

### Step 5 - set runtime configuration

Development defaults already work. Explicit PowerShell examples:

```powershell
$env:MOCK_INTERVIEW_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
$env:MOCK_INTERVIEW_OLLAMA_INTERACTIVE_MODEL = "qwen3:1.7b"
$env:MOCK_INTERVIEW_OLLAMA_REPORT_MODEL = "qwen3:1.7b"
$env:MOCK_INTERVIEW_OLLAMA_CONTEXT_TOKENS = "4096"
$env:MOCK_INTERVIEW_OLLAMA_MAX_OUTPUT_TOKENS = "1024"
$env:MOCK_INTERVIEW_OLLAMA_THINK = "false"
$env:MOCK_INTERVIEW_WHISPER_MODEL = "large-v3-turbo"
$env:MOCK_INTERVIEW_WHISPER_DEVICE = "cpu"
$env:MOCK_INTERVIEW_WHISPER_COMPUTE_TYPE = "int8"
$env:MOCK_INTERVIEW_WHISPER_BEAM_SIZE = "1"
$env:MOCK_INTERVIEW_TTS_BACKEND = "auto"
```

Production must set `MOCK_INTERVIEW_ALLOW_DETERMINISTIC_FALLBACK=false`, use
CUDA STT settings, configure the model gateway, and inject environment values
through the deployment secret/config system.

### Live-turn latency

Submitting an answer is synchronous in the current MVP. The request must
evaluate the answer, generate a non-repeated adaptive question, store both
records, and then return the next URL. On the tested CPU, the earlier two-call
`qwen3:8b` path took about 146 seconds. The current implementation:

1. uses one combined structured call;
2. uses `qwen3:1.7b` for the interactive path;
3. limits live context to 3,072 tokens and generated output to 384 tokens;
4. keeps the model loaded for 30 minutes;
5. prevents repeated questions; and
6. falls back directly to bounded deterministic results after a local-model
   failure instead of launching two additional model calls.

The same measured example completed in about 30 seconds on CPU. This is an MVP
development result, not a production service-level target. For a natural
multi-student experience, move inference to GPU workers and use the asynchronous
queue/status flow in section 25.

### Final-answer and report latency

The original final-answer request performed both operations before returning:

1. evaluate the last answer with `qwen3:1.7b` (measured at about 40 seconds);
2. generate the complete report with `qwen3:8b` (measured at about 101 seconds).

This made the last submit button appear stuck for roughly 140 seconds even
though the answers were stored correctly. The completion workflow now uses
explicit states:

```text
in_progress
  -> final answer evaluated
  -> completed
  -> browser opens the processing screen
  -> processing screen starts report generation
  -> evaluating
  -> report_ready
  -> browser opens the report
```

The final-question button is labelled **Submit final answer and finish
interview**. Report generation is no longer performed inside the final-answer
request. CPU development uses `qwen3:1.7b` for report wording; the measured
three-answer report took about 27 seconds while preserving the already
validated scores and evidence. A GPU production deployment can set
`MOCK_INTERVIEW_OLLAMA_REPORT_MODEL=qwen3:8b`.

### Step 6 - review and apply database migrations

Review first:

```powershell
python manage.py showmigrations mock_interview
python manage.py makemigrations mock_interview --check --dry-run
```

Apply during an approved maintenance window:

```powershell
python manage.py migrate mock_interview
```

Migration `0003` replaces the incorrect default-Django-user foreign key with
the ERP student's stable `Employee_id`. This avoids a cross-database foreign
key to the unmanaged `control_room_user` table.

### Step 7 - verify before starting

```powershell
python manage.py test mock_interview.tests --settings=mock_interview.tests.settings --verbosity 2
python manage.py check --settings=mock_interview.tests.settings
python -m compileall -q mock_interview
```

Expected test result for this implementation: 20 tests, all passing.

### Step 8 - run development mode

```powershell
python manage.py runserver
```

Open `/mock-interview/` while logged in as a student. Follow:

1. Upload a PDF/DOCX resume or continue without one.
2. Configure role, round, difficulty, skills, and job description.
3. Confirm browser devices and local AI readiness.
4. Accept recording/data consent.
5. Listen to one stored question.
6. Record, stop, review the original STT result, and submit.
7. Repeat, skip, or end after at least one evaluated answer.
8. Review the report and download its private PDF.

For access from another device, HTTPS is mandatory because browsers restrict
camera/microphone APIs on insecure origins.

## 24. Implemented file tree and responsibilities

```text
mock_interview/
|-- admin.py                         Admin inspection of real records
|-- ai/
|   |-- interview_engine.py         Prompts, fallback, scoring preparation
|   |-- ollama_client.py            Local structured-output client/readiness
|   `-- schemas.py                  JSON schemas for every AI response
|-- migrations/
|   |-- 0001_initial.py
|   |-- 0002_functional_interview_domain.py
|   |-- 0003_erp_student_identity.py
|   `-- 0004_answer_submission_state.py
|-- models/__init__.py              Resume/session/question/answer/report data
|-- services/
|   |-- access.py                   Student role and Employee_id boundary
|   |-- interview_service.py        Transactions and interview state machine
|   `-- resume_parser.py            PDF/DOCX validation and extraction
|-- speech/
|   |-- metrics.py                  Deterministic speaking metrics
|   |-- stt.py                      Faster-Whisper loader/transcription
|   `-- tts.py                      Kokoro/Piper selection and WAV generation
|-- static/mock_interview/css/style.css
|-- templates/mock_interview/       Seven real student workflow pages
|-- tests/
|   |-- settings.py                 Isolated SQLite test configuration
|   |-- test_services.py            Access/resume/scoring/model fallback
|   |-- test_urls.py                UUID and runtime routes
|   `-- test_workflow.py            Database-backed state/audio workflow
|-- urls.py                         Private pages and JSON/media endpoints
|-- views/dashboard.py              HTTP validation and ownership-filtered views
`-- README.md                       Architecture, operation, and deployment
```

Only these integration files outside the app are required:

- `rit_academic_system/settings.py`: local model/runtime limits
- `rit_academic_system/urls.py`: `/mock-interview/` include
- `requirements.txt`: open-source inference/parser dependencies
- `.gitignore`: excludes downloaded Piper weights and generated media

## 25. Production architecture for up to 1,000 connected students

One thousand connected students is not the same as one thousand simultaneous
LLM/STT jobs. Capacity must be based on measured peak inference jobs per second,
answer duration, acceptable queue delay, and target p95 latency.

Required production flow:

```text
Browser
  -> HTTPS load balancer/WAF
  -> replicated Django ASGI/API pods
  -> MariaDB primary plus read replicas
  -> private S3-compatible object storage
  -> RabbitMQ durable queues
       |-> resume workers
       |-> GPU STT workers
       |-> LLM gateway -> replicated vLLM workers
       |-> CPU/GPU TTS workers
       `-> report/PDF workers
  -> Valkey for cache, rate limits, and short-lived status
  -> Prometheus/Grafana/Loki or institution equivalents
```

Deployment mode by component:

| Component | Development mode | Production mode |
|---|---|---|
| Django | `runserver`, synchronous | ASGI pods, autoscaled, no inference in web workers |
| LLM | Ollama `qwen3:1.7b` live turns and reports | vLLM `qwen3:8b` continuous batching, GPU replicas, bounded queues |
| STT | Faster-Whisper Small CPU | `large-v3-turbo` CUDA worker pool |
| TTS | Piper or Kokoro in process | Pre-warmed replicated TTS service |
| Tasks | Inline MVP calls | Celery workers with RabbitMQ quorum queues |
| Cache | Optional | Valkey cluster/sentinel |
| Files | Django local media | Private S3-compatible object storage |
| Database | Existing development DB | MariaDB HA, pooling, backups, tested restore |

Production steps:

1. Benchmark one STT, LLM, and TTS worker with realistic student audio.
2. Record p50/p95 latency, GPU memory, jobs/second, and failure rate.
3. Use those measurements to calculate replicas; do not guess GPU count.
4. Add Celery task wrappers around the existing service functions. Keep
   ownership and business rules in the current services.
5. Return `202 Accepted` plus a job UUID for transcription, evaluation, next
   question generation, and reports; poll or push status to the browser.
6. Configure separate queues and concurrency limits so a report burst cannot
   starve live STT.
7. Apply per-student session/rate limits and global admission control.
8. Pre-warm model workers before the event and keep at least one spare replica.
9. Run staged tests at 50, 100, 250, 500, and 1,000 connected virtual users
   with representative audio upload sizes.
10. Deploy only if p95 targets, queue depth, error rate, and GPU utilization stay
    within the agreed limits.

Suggested service-level objectives:

- API requests not performing inference: p95 under 500 ms
- Audio upload acknowledgement: p95 under 2 seconds after upload completes
- STT result: p95 under 15 seconds for a 90-second answer
- Next spoken question: p95 under 10 seconds after evaluation
- No loss of accepted audio or completed evaluation jobs

The current synchronous MVP must not be declared 1,000-user ready. A real
Qwen 3 8B interviewer-schema request on this development computer took about
35 seconds even with thinking disabled; the first model-load request took about
99 seconds. Production sign-off requires the queue work, GPU benchmarks,
security testing, faculty evaluation calibration, and the staged load test
above.

## 26. Deployment and operations checklist

Before production:

- Run Linux containers; do not use Django `runserver`.
- Terminate TLS at the ingress and enable secure cookies, CSRF trusted origins,
  HSTS, and restrictive allowed hosts.
- Remove all hard-coded secrets from project settings and rotate any exposed
  key before deployment.
- Store resumes/audio/reports privately and deliver only through authorized
  short-lived responses.
- Add ClamAV scanning and reject a resume until scanning succeeds.
- Define retention periods and a deletion workflow for resume, audio,
  transcripts, and reports.
- Disable deterministic AI fallback in production.
- Pin image and model digests and record licences/checksums.
- Use database migrations as a controlled release job, never from every pod.
- Back up MariaDB and object storage; perform a restore drill.
- Alert on queue age, failed jobs, inference latency, GPU memory, disk,
  database connections, and HTTP 5xx rate.
- Use canary deployment and keep the previous image for rollback.
- Do not use AI scores for placement eligibility, grading, or hiring.

Release gates:

1. Unit/integration tests pass.
2. Security and permission tests pass.
3. Faculty-scored benchmark agreement is approved.
4. Load and soak tests pass on production-sized hardware.
5. Backup/restore and rollback are demonstrated.
6. A small consenting pilot completes before the campus-wide event.

