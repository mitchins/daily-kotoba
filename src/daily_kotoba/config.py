"""Runtime configuration, loaded from `KOTOBA_*` environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LEVELS = {"N1", "N2", "N3", "N4", "N5"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KOTOBA_")

    db_path: str = "/data/kotoba.db"
    seed_db: str = "/app/seed/kotoba-seed.db"
    cache_dir: str = "/data/cache"
    cache_keep_days: int = 7
    levels: str = "N5,N4,N3,N2,N1"
    font_dir: str = "/app/fonts"
    log_level: str = "INFO"

    @property
    def levels_list(self) -> list[str]:
        levels = [lvl.strip().upper() for lvl in self.levels.split(",") if lvl.strip()]
        invalid = [lvl for lvl in levels if lvl not in _VALID_LEVELS]
        if invalid:
            raise ValueError(f"invalid KOTOBA_LEVELS entries: {invalid}; must be one of N1-N5")
        return levels


def get_settings() -> Settings:
    return Settings()
