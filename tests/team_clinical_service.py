"""Loopback-only harness mounting the unmodified SecretarIA provisioning router."""

import os
import sys

os.environ.update(
    DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55458/task058_secretaria",
    APP_ENV="dev",
    INTERNAL_API_KEY="task058-local-only",
    OPENAI_API_KEY="",
    GOOGLE_CLIENT_ID="",
    GOOGLE_CLIENT_SECRET="",
    SMTP_HOST="",
    META_APP_SECRET="",
)
sys.path.insert(0, "C:/TECH/BRAIN/secretarIA/src")
from fastapi import FastAPI
from secretaria.api.internal_provisioning import router

app = FastAPI()
app.include_router(router)
counts = {"mutations": 0}


@app.middleware("http")
async def count_mutations(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        counts["mutations"] += 1
    return await call_next(request)


@app.get("/__qa/counts")
def qa_counts():
    return counts
