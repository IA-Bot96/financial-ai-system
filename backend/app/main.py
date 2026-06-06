"""FastAPI entrypoint."""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import extraction, fie
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.engines.fie import bootcheck

settings = get_settings()
configure_logging(settings.debug)

# Fail-fast contract-integrity check: a mis-wired formula / authority matrix /
# taxonomy / citation contract aborts startup loudly instead of serving wrong answers.
_results = bootcheck.assert_contracts()
logging.getLogger("app.engines.fie").info(
    "FIE contract integrity OK (%d checks)", len(_results),
    extra={"component": "bootcheck"})

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],  # Angular dev server; tighten for prod
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(extraction.router, prefix="/api")
app.include_router(fie.router, prefix="/api")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
