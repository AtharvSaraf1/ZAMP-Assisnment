"""Settings, read once from environment variables and the project's .env file."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    gemini_base_url: str
    data_dir: Path
    master_dir: Path
    samples_dir: Path
    web_dir: Path

    @property
    def ai_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def llm_cache_dir(self) -> Path:
        # Shared across data folders (tests use a temporary one) so a file is only sent to the AI once.
        return Path(os.environ.get("LLM_CACHE_DIR", ROOT / "data" / "llm_cache"))


@lru_cache
def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    settings = Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_STRONG_MODEL", "gemini-3.6-flash"),
        gemini_base_url="https://generativelanguage.googleapis.com/v1beta",
        data_dir=data_dir,
        master_dir=ROOT / "data" / "master",
        samples_dir=ROOT / "samples" / "invoices",
        web_dir=ROOT / "web",
    )
    for folder in (settings.data_dir, settings.uploads_dir, settings.llm_cache_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return settings
