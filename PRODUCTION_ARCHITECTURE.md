tag: production-ready-architecture

# Production-Ready Architecture for Mock Interview Standalone

This document outlines a production-ready architecture for this Django-based AI Mock Interview system, based on the actual codebase. This is tailored to the current stack (Django 5.x, SQLite/Postgres, Ollama, Whisper/Faster-Whisper, Kokoro/Piper TTS, Qdrant, background worker).

## 1) Architecture Overview

```text
[Users/Browser (HTTPS)] 
        ↓
[Nginx Reverse Proxy (TLS, static/media, rate limiting)]
        ↓
[Gunicorn (WSGI) x N workers]
        ├── Django App (Views/Services/Models)
        └── Interview Worker (separate systemd service recommended)
                ↓
[Postgres DB]  [Redis (optional: cache/rate limit/sessions)]
                ↓
[Local/Network AI Services]
  ├── Ollama (LLM: question/eval/report) with model failover
  ├── Faster-Whisper (STT)
  ├── Kokoro/Piper (TTS)
  └── Qdrant (Vector DB for RAG, optional)
                ↓
[File Storage: media/ (resumes, audio, PDFs, reports)]
```

## 2) Environment Variables (.env.example)

Copy this to `.env` and fill in your values. Never commit this file.

```env
# ========================
# Django Core Settings
# ========================
MOCK_INTERVIEW_SECRET_KEY=CHANGE_ME_TO_A_LONG_RANDOM_64_CHAR_STRING
MOCK_INTERVIEW_DEBUG=false
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com,YOUR_SERVER_IP

# ========================
# Database (PostgreSQL)
# ========================
DB_NAME=mock_interview
DB_USER=mock_interview_user
DB_PASSWORD=CHANGE_ME_STRONG_PASSWORD
DB_HOST=127.0.0.1
DB_PORT=5432
DATABASE_URL=postgres://mock_interview_user:CHANGE_ME_STRONG_PASSWORD@127.0.0.1:5432/mock_interview

# ========================
# AI/LLM Configuration (Ollama)
# ========================
MOCK_INTERVIEW_DUMMY_MODE=false
MOCK_INTERVIEW_OLLAMA_BASE_URL=http://127.0.0.1:11434
MOCK_INTERVIEW_OLLAMA_MODEL=qwen3:8b
MOCK_INTERVIEW_OLLAMA_INTERACTIVE_MODEL=qwen3:1.7b
MOCK_INTERVIEW_OLLAMA_REPORT_MODEL=qwen3:8b
MOCK_INTERVIEW_OLLAMA_TIMEOUT=120
MOCK_INTERVIEW_OLLAMA_CONTEXT_TOKENS=4096
MOCK_INTERVIEW_OLLAMA_MAX_OUTPUT_TOKENS=1024
MOCK_INTERVIEW_OLLAMA_THINK=false

# Model Failover Routes (comma separated)
MOCK_INTERVIEW_LLM_ROUTE_QUESTION=qwen2.5:7b-instruct,llama-3.1:8b,mistral:7b-instruct
MOCK_INTERVIEW_LLM_ROUTE_INTERACTIVE=qwen2.5:7b-instruct,llama-3.1:8b,mistral:7b-instruct
MOCK_INTERVIEW_LLM_ROUTE_EVALUATION=qwen2.5:7b-instruct,llama-3.1:8b,mistral:7b-instruct
MOCK_INTERVIEW_LLM_ROUTE_REPORT=qwen3:8b,qwen2.5:7b-instruct,llama-3.1:8b
MOCK_INTERVIEW_LLM_ROUTE_DEFAULT=qwen2.5:7b-instruct,llama-3.1:8b

# ========================
# Speech (STT/TTS)
# ========================
MOCK_INTERVIEW_DUMMY_STT=false
MOCK_INTERVIEW_DUMMY_TTS=false
MOCK_INTERVIEW_WHISPER_MODEL=large-v3-turbo
MOCK_INTERVIEW_WHISPER_DEVICE=cpu
MOCK_INTERVIEW_WHISPER_COMPUTE_TYPE=int8
MOCK_INTERVIEW_WHISPER_BEAM_SIZE=1
MOCK_INTERVIEW_KOKORO_VOICE=af_heart
MOCK_INTERVIEW_TTS_BACKEND=auto

# ========================
# RAG (Optional)
# ========================
RAG_QDRANT_URL=http://127.0.0.1:6333
RAG_QDRANT_COLLECTION=mock_interview_documents
RAG_EMBEDDING_MODEL=BAAI/bge-m3
RAG_EMBEDDING_DIMENSION=1024

# ========================
# Background Worker & Pipeline
# ========================
MOCK_INTERVIEW_FAST_INTERVIEW_PIPELINE=true
MOCK_INTERVIEW_INTERVIEW_WORKER_AUTO_START=false
MOCK_INTERVIEW_INTERVIEW_WORKER_INTERVAL=2.0
MOCK_INTERVIEW_INTERVIEW_EVAL_STALE_SECONDS=600
MOCK_INTERVIEW_INTERVIEW_EVAL_MAX_ATTEMPTS=3

# ========================
# Security & Limits
# ========================
MOCK_INTERVIEW_ALLOW_DETERMINISTIC_FALLBACK=true
MOCK_INTERVIEW_MAX_RESUME_BYTES=5242880
MOCK_INTERVIEW_MAX_ANSWER_AUDIO_BYTES=26214400
```

## 3) Django Settings for Production

Update `mock_interview_site/settings.py` to use environment variables and Postgres. Add `dj-database-url` to requirements.

```python
import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("MOCK_INTERVIEW_SECRET_KEY")
DEBUG = os.environ.get("MOCK_INTERVIEW_DEBUG", "false").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",") if os.environ.get("ALLOWED_HOSTS") else []

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL"),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Security headers
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
```

## 4) Gunicorn Configuration

Create `/var/www/mock-interview/gunicorn.conf.py`:

```python
import multiprocessing

bind = "unix:/var/run/mockinterview/gunicorn.sock"
workers = multiprocessing.cpu_count() * 2 + 1
threads = 2
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 50

accesslog = "/var/log/mockinterview/gunicorn-access.log"
errorlog = "/var/log/mockinterview/gunicorn-error.log"
loglevel = "info"

preload_app = True
```

## 5) Systemd Services

### Gunicorn Service
Create `/etc/systemd/system/mockinterview-gunicorn.service`:

```ini
[Unit]
Description=Mock Interview Gunicorn Daemon
After=network.target postgresql.service ollama.service
Requires=postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/mock-interview
EnvironmentFile=/var/www/mock-interview/.env
ExecStart=/var/www/mock-interview/.venv/bin/gunicorn -c gunicorn.conf.py mock_interview_site.wsgi:application
ExecReload=/bin/kill -s HUP $MAINPID
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

### Worker Service
Create `/etc/systemd/system/mockinterview-worker.service`:

```ini
[Unit]
Description=Mock Interview Background Worker
After=network.target postgresql.service ollama.service
Requires=postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/mock-interview
EnvironmentFile=/var/www/mock-interview/.env
ExecStart=/var/www/mock-interview/.venv/bin/python manage.py run_interview_worker
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mockinterview-gunicorn
sudo systemctl enable --now mockinterview-worker
```

## 6) Nginx Configuration

Create `/etc/nginx/sites-available/mockinterview`:

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com www.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    client_max_body_size 25M;

    location /static/ {
        alias /var/www/mock-interview/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    location /media/ {
        alias /var/www/mock-interview/media/;
        expires 7d;
        add_header Cache-Control "public";
    }

    location / {
        proxy_pass http://unix:/var/run/mockinterview/gunicorn.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
        proxy_connect_timeout 60s;
    }
}
```

Enable: 

```bash
sudo ln -s /etc/nginx/sites-available/mockinterview /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 7) Deployment Script

Create `deploy.sh`:

```bash
#!/bin/bash
set -e

echo "[DEPLOY] Starting..."
cd /var/www/mock-interview
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
sudo systemctl restart mockinterview-gunicorn
sudo systemctl restart mockinterview-worker
echo "[DEPLOY] Complete!"
```

```bash
chmod +x deploy.sh
```

## 13) Load Balancing, Guardrails, PII, Semantic Caching & Production Techniques

### 13.1) Load Balancing

#### When to do it
| Situation | Action |
| :-- | :-- |
| < 50 users on 1 VM | Skip. Gunicorn is enough. |
| 50+ concurrent users or need zero-downtime deploys | Consider load balancing. |
| Multiple VMs/servers | Mandatory. |

#### Prerequisites
| Item | What to do | Notes |
| :-- | :-- | :-- |
| Shared Database | Use PostgreSQL (never SQLite) | Required for multi-node writes. |
| Shared Media | Use NFS or S3/MinIO for `media/` | Files must be accessible by all app nodes. |
| Shared Sessions | Use Redis for Django sessions | Prevents logouts across nodes. |
| Single Worker Instance | Run only 1 `mockinterview-worker` total | Never run on every app node. |
| Environment Parity | Same code, `.env`, models on all nodes | Avoid inconsistencies. |

#### Techniques
| Technique | Explanation | When to use |
| :-- | :-- | :-- |
| Round Robin | Send requests in order | Simple starting point. |
| Least Connections | Send to node with fewest active connections | **Recommended** for this app (long requests). |
| IP Hash/Sticky Sessions | Pin user to same node | Only if shared sessions not possible. Prefer shared sessions. |
| Health Checks | Auto-remove unhealthy nodes | Mandatory. |
| Graceful Failover | Drain connections on deploy | Needed for zero-downtime. |

#### Nginx Upstream Example
```nginx
upstream mockinterview_backend {
    least_conn;
    server 10.0.0.10:8000 max_fails=3 fail_timeout=30s;
    server 10.0.0.11:8000 max_fails=3 fail_timeout=30s;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    location / {
        proxy_pass http://mockinterview_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
        proxy_connect_timeout 60s;
    }
}
```

#### Steps
1. Setup shared Postgres, shared media, Redis sessions.
2. Add second app server with identical setup.
3. Run Gunicorn on both (only one worker).
4. Put Nginx LB in front.
5. Test failover.
6. Monitor DB locks, latency, worker health.

### 13.2) Guardrails
| Guardrail | What it does | Implementation |
| :-- | :-- | :-- |
| Input Validation | Trim/reject bad inputs | Enforce max lengths on text/uploads. |
| Prompt Injection Defense | Treat user data as untrusted | Keep strict system prompts, sanitize fields. |
| Schema Enforcement | Force valid JSON | Use existing schema validation, retry on failure. |
| Content Filtering | Block abusive content | Lightweight local filter. |
| Output Guardrails | Validate keys/types/ranges | Post-check LLM outputs, fallback if invalid. |
| PII Redaction Gate | Redact before logging/sending | Apply redaction utility. |
| Rate Limiting | Prevent abuse | Nginx `limit_req` + Django limits. |
| Model Allowlist | Restrict models/routes | Only use configured routes. |

### 13.3) PII Handling
| Technique | Explanation | Implementation |
| :-- | :-- | :-- |
| Data Minimization | Send only what's needed | Avoid dumping full data to logs/prompts. |
| PII Detection & Redaction | Find/redact emails/phones/IDs | Add `mock_interview/utils/pii.py` with regex. |
| Encryption at Rest/Transit | Protect data | Restrict permissions, use HTTPS, encrypt backups. |
| Access Control | Enforce ownership | Audit role-based views. |
| Safe Logging | No raw PII | Log IDs/statuses only. |
| Consent Tracking | Track consent | Use `consented_at`/`consent_version`. |
| Data Retention/Deletion | Purge old data | Add cleanup management command. |

### 13.4) Semantic Caching
| Technique | Explanation | Recommendation |
| :-- | :-- | :-- |
| Local Embeddings | Embed locally | `sentence-transformers` (`all-MiniLM-L6-v2`) or `bge-m3`. |
| Similarity Threshold | Hit threshold | Start 0.9 (stricter 0.93 for evaluations). |
| Smart Cache Keys | Include context | Key by (role, schema hash, normalized prompt, context). |
| TTL & LRU | Expire/prune | TTL 7-30 days. |
| Cache Only Successes | Never cache errors | Only cache valid JSON from successful calls. |
| Bypass Rules | Skip unique/sensitive | Add `no_cache` and avoid caching highly unique PII-heavy prompts. |
| Leak Prevention | Prevent cross-user hits | Scope keys carefully. |

**Implementation:** Create `mock_interview/ai/semantic_cache.py`, use Qdrant collection `mock_interview_semantic_cache`, wrap `chat_json` with lookup/store, add metrics.

### 13.5) Master Production Checklist
| Category | Priority | Items |
| :-- | :-- | :-- |
| Infrastructure/Reliability | P0 | HTTPS, Postgres, separate worker, Gunicorn, Nginx static/media, env config, backups, firewall |
| Security | P0 | Secure headers, guardrails, PII redaction/safe logging, rate limiting, upload validation, secrets management |
| Performance/Scaling | P1 | Load balancing (when needed), Redis sessions/cache, semantic caching, DB optimization, connection pooling, LLM failover |
| Observability/Ops | P1 | Health checks, centralized/redacted logging, error tracking, metrics/alerts, LLM monitoring, automated deploys, DR plan |

**Golden Rule:** Don't load balance until shared storage/sessions + single worker are in place. Don't add semantic caching without guardrails. Don't handle production PII without redaction and access control. For 1-50 users, prioritize P0 items. The LLM failover fixes are production-ready once the infrastructure above is met.