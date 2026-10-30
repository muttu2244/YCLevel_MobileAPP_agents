"""Runtime configuration.

Every value has a default that works on a laptop with no network and no
database server, so `python -m vachan demo` runs straight after checkout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# vachan/agent/vachan/config.py -> vachan/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
SAMPLES_DIR = PROJECT_ROOT / "samples"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader (KEY=VALUE, # comments). Real hosts use real secrets."""
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    mode: str = "mock"          # mock | ollama | openai
    model: str = "llama3.1"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    database_url: str = "sqlite:///./vachan.db"
    timezone: str = "Asia/Kolkata"
    business_name: str = "Sharma Interiors"
    owner_name: str = "Rakesh (me)"

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    @property
    def sqlite_path(self) -> str:
        return self.database_url.split("sqlite:///")[-1]


def settings() -> Settings:
    load_dotenv()
    return Settings(
        mode=os.getenv("VACHAN_LLM_MODE", "mock").strip().lower(),
        model=os.getenv("VACHAN_MODEL", "llama3.1"),
        base_url=os.getenv("VACHAN_BASE_URL", "http://localhost:11434"),
        api_key=os.getenv("VACHAN_API_KEY", ""),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./vachan.db"),
        timezone=os.getenv("VACHAN_TIMEZONE", "Asia/Kolkata"),
        business_name=os.getenv("VACHAN_BUSINESS", "Sharma Interiors"),
        owner_name=os.getenv("VACHAN_OWNER", "Rakesh (me)"),
    )
