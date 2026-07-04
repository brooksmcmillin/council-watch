from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Required
    anthropic_api_key: str = ""

    # Summarization model (env-overridable via SUMMARIZATION_MODEL)
    summarization_model: str = "claude-sonnet-5"

    # Database
    database_url: str = "sqlite:///data/council.db"
    pdf_storage_dir: str = "data/pdfs"

    # SMTP email (all optional — skip email if blank)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    # Comma-separated static recipients. Optional now that delivery is
    # subscriber-driven; kept as a fallback/admin channel — these addresses
    # always receive notifications (no unsubscribe link).
    email_to: str = ""

    # Bluesky (optional — skip if blank)
    bluesky_handle: str = ""
    bluesky_app_password: str = ""

    # App
    scrape_interval_minutes: int = 60
    app_base_url: str = "http://localhost:8000"

    # Historical backfill: periodically re-scrape prior years so late-posted
    # minutes and older meetings are captured without manual runs.
    backfill_years: int = 2  # number of prior years to re-scrape (0 disables)
    backfill_interval_hours: int = 168  # default weekly

    @property
    def email_enabled(self) -> bool:
        # Recipients come from the subscribers table (plus the optional static
        # email_to list), so the channel is enabled on SMTP config alone.
        return bool(self.smtp_host and self.email_from)

    @property
    def bluesky_enabled(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_app_password)


settings = Settings()
