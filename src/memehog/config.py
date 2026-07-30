from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = ""
    allowed_telegram_ids: str = ""
    api_token: str = "change-me"
    host: str = "0.0.0.0"
    port: int = 2137
    data_dir: Path = Path("data")
    cookies_file: str = ""
    scan_cron: str = "0 3 * * *"
    log_level: str = "INFO"

    # Nightly VLM indexer: any OpenAI-compatible chat-completions endpoint with
    # vision (Gemini, OpenRouter, Groq, Mistral, local Ollama, ...). Both
    # base_url and model must be set for indexing to run.
    vlm_base_url: str = ""
    vlm_api_key: str = ""
    vlm_model: str = ""
    vlm_language: str = "English"
    # Requests per minute the indexer may use (free tiers are tight).
    vlm_rpm: float = 10
    # Cap per nightly run, so a big backfill spreads over several nights
    # instead of burning a daily free-tier quota in one go.
    vlm_max_per_run: int = 200
    # Send spicy memes to the VLM too? Free API tiers may use inputs for
    # training, so this is opt-in.
    vlm_index_spicy: bool = False

    # Baked into the Docker image by CI (see docker/Dockerfile); "dev" locally.
    memehog_build_sha: str = "dev"
    memehog_build_date: str = ""

    @property
    def vlm_enabled(self) -> bool:
        return bool(self.vlm_base_url and self.vlm_model)

    @property
    def allowed_ids(self) -> set[int]:
        return {
            int(part)
            for part in self.allowed_telegram_ids.replace(";", ",").split(",")
            if part.strip().lstrip("-").isdigit()
        }

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def pending_dir(self) -> Path:
        """Quarantine for guest submissions awaiting the owner's vote."""
        return self.data_dir / "pending"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memehog.db"

    @property
    def cookies_path(self) -> Path | None:
        return Path(self.cookies_file) if self.cookies_file else None

    def ensure_dirs(self) -> None:
        for d in (self.library_dir, self.thumbs_dir, self.tmp_dir, self.pending_dir):
            d.mkdir(parents=True, exist_ok=True)
