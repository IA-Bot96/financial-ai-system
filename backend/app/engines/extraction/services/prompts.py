"""Load and render versioned prompt templates from the prompts/ directory.

File format:
    SYSTEM:
    <system text>

    USER:
    <user text with {placeholders}>
"""
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@lru_cache
def _load_raw(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def load_prompt(name: str) -> tuple[str, str]:
    raw = _load_raw(name)
    if "SYSTEM:" in raw and "USER:" in raw:
        after = raw.split("SYSTEM:", 1)[1]
        system, user = after.split("USER:", 1)
        return system.strip(), user.strip()
    return "", raw.strip()


def render(name: str, **kwargs) -> tuple[str, str]:
    system, user = load_prompt(name)
    return system, user.format(**kwargs)
