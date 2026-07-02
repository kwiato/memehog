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
    port: int = 8080
    data_dir: Path = Path("data")
    cookies_file: str = ""
    scan_cron: str = "0 3 * * *"
    log_level: str = "INFO"

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
    def db_path(self) -> Path:
        return self.data_dir / "memehog.db"

    @property
    def cookies_path(self) -> Path | None:
        return Path(self.cookies_file) if self.cookies_file else None

    def ensure_dirs(self) -> None:
        for d in (self.library_dir, self.thumbs_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)
