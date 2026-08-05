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
    # Let the indexer also attach tags to memes (marked as AI tags in the UI).
    vlm_auto_tag: bool = True
    # Extra indexer runs between the nightly ones, in minutes (0 = nightly
    # only). Frequent runs help with free-tier daily quotas that reset at
    # odd hours — a model that hit its limit gets retried soon after.
    vlm_interval_minutes: int = 60

    # Meme crawler: daily batch for the swipe inbox. Sources are one per
    # line: "reddit:<subreddit>" or "rss:<feed url>". Empty list = crawler
    # off. All overridable from the settings UI.
    crawler_sources: str = ""
    crawler_daily_target: int = 120
    # Hour (local) at which the daily crawl runs.
    crawler_hour: int = 6
    # Reddit blocks anonymous listing requests from many networks. A free
    # "script" app (reddit.com/prefs/apps) fixes that: with credentials set,
    # the crawler uses the official OAuth API instead.
    crawler_reddit_client_id: str = ""
    crawler_reddit_secret: str = ""

    # Public feed (requests arriving through the reverse proxy with the
    # X-Memehog-Public header): free memes per visitor per day, and how many
    # extra a "Feed the hog!" upload unlocks.
    public_daily_limit: int = 30
    public_unlock_credits: int = 200

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
    def candidates_dir(self) -> Path:
        """Thumbnails of crawled memes waiting in the swipe inbox."""
        return self.data_dir / "candidates"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memehog.db"

    @property
    def cookies_path(self) -> Path | None:
        return Path(self.cookies_file) if self.cookies_file else None

    def ensure_dirs(self) -> None:
        for d in (self.library_dir, self.thumbs_dir, self.tmp_dir,
                  self.pending_dir, self.candidates_dir):
            d.mkdir(parents=True, exist_ok=True)
