from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Required
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite:///data/council.db"
    pdf_storage_dir: str = "data/pdfs"

    # SMTP email (all optional — skip email if blank)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""  # comma-separated recipients

    # Bluesky (optional — skip if blank)
    bluesky_handle: str = ""
    bluesky_app_password: str = ""

    # App
    scrape_interval_minutes: int = 60
    app_base_url: str = "http://localhost:8000"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.email_from and self.email_to)

    @property
    def bluesky_enabled(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_app_password)


settings = Settings()
