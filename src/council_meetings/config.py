from ipaddress import IPv4Network, IPv6Network
from typing import Literal
from urllib.parse import urljoin

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings


def resolve_public_url(base_url: str, url: str | None) -> str:
    """Resolve published hyperlinks without admitting executable URL schemes."""
    if not url:
        return ""
    try:
        resolved = httpx.URL(urljoin(base_url.rstrip("/") + "/", url))
    except (ValueError, httpx.InvalidURL):
        return ""
    if resolved.scheme not in {"http", "https"} or not resolved.host or resolved.userinfo:
        return ""
    return str(resolved)


class _SharedEnvSettings(BaseSettings):
    """Base for the two settings models that co-read the shared ``.env`` file.

    ``Settings`` (unprefixed) and ``CityConfig`` (``CITY_`` prefix) both parse
    the *entire* dotenv file, so each sees the other's keys. Ignore unmatched
    keys instead of pydantic-settings' default ``extra="forbid"``, which would
    otherwise crash at import time (``settings``/``city`` are built eagerly
    below) the moment a ``.env`` holds the sibling model's variables.
    """

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class CityConfig(_SharedEnvSettings):
    """Per-city configuration for the CivicPlus source being monitored.

    Everything Campbell-specific lives here so additional CivicPlus cities can
    be added without forking the scraper/summarizer/notifier code — override any
    field via a ``CITY_``-prefixed env var (e.g. ``CITY_BASE_URL``,
    ``CITY_CATEGORY_ID``). Defaults describe the City of Campbell, CA.
    """

    model_config = {**_SharedEnvSettings.model_config, "env_prefix": "CITY_"}

    # Short name used in email subjects and the bot User-Agent (e.g. "Campbell").
    name: str = "Campbell"
    # City + state as it should read inside the summarizer prompts
    # (e.g. "Campbell, California").
    location: str = "Campbell, California"
    # Human display name for notifications (e.g. "Campbell City Council").
    display_name: str = "Campbell City Council"

    # CivicPlus site root, no trailing slash (e.g. "https://www.campbellca.gov").
    base_url: str = "https://www.campbellca.gov"
    # AgendaCenter category path segment (e.g. "City-Council-10").
    agenda_path: str = "City-Council-10"
    # CivicPlus category id (the ``catID`` used by the backfill endpoint).
    category_id: str = "10"

    # None enables Campbell's current portal only for Campbell's legacy category.
    # An empty string disables eSCRIBE when using a CivicPlus-only city.
    escribe_base_url: str | None = None

    @property
    def escribe_url(self) -> str:
        if self.escribe_base_url is not None:
            return self.escribe_base_url.rstrip("/")
        if self.base_url.rstrip("/") == "https://www.campbellca.gov" and self.category_id == "10":
            return "https://pub-campbell.escribemeetings.com"
        return ""

    # Identifies the bot to the city's servers.
    user_agent: str = (
        "CampbellCouncilMonitor/1.0 (+https://github.com/brooksmcmillin/council-meetings)"
    )

    @property
    def agenda_center_url(self) -> str:
        return f"{self.base_url}/AgendaCenter/{self.agenda_path}"

    def resolve_url(self, url: str | None) -> str:
        """Resolve safe legacy, portal and video links, including existing saved rows."""
        return resolve_public_url(self.base_url, url)

    @property
    def backfill_url(self) -> str:
        return f"{self.base_url}/AgendaCenter/UpdateCategoryList"


class Settings(_SharedEnvSettings):
    llm_mode: Literal["direct", "gateway"] = "direct"
    anthropic_api_key: str = ""
    llm_gateway_url: str = ""
    llm_gateway_api_key: SecretStr = SecretStr("")

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
    # Only these socket peers may supply Cloudflare visitor identities.
    trusted_proxy_networks: tuple[IPv4Network | IPv6Network, ...] = ()

    # Historical backfill: periodically re-scrape prior years so late-posted
    # minutes and older meetings are captured without manual runs.
    backfill_years: int = 2  # number of prior years to re-scrape (0 disables)
    backfill_interval_hours: int = 168  # default weekly

    def summarization_route(self) -> tuple[str | None, str] | None:
        """Return the selected base URL and key; only unconfigured direct mode may skip."""
        if self.llm_mode == "direct":
            key = self.anthropic_api_key.strip()
            return (None, key) if key else None

        key = self.llm_gateway_api_key.get_secret_value().strip()
        raw_url = self.llm_gateway_url.strip()
        if not raw_url or not key:
            raise ValueError("LLM_MODE=gateway requires LLM_GATEWAY_URL and LLM_GATEWAY_API_KEY")
        try:
            url = httpx.URL(raw_url)
        except httpx.InvalidURL:
            raise ValueError("LLM_GATEWAY_URL must be a valid HTTPS base URL") from None
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError("LLM_GATEWAY_URL must be a valid HTTPS base URL")
        if not url.raw_path.rstrip(b"/").endswith(b"/anthropic"):
            raise ValueError("LLM_GATEWAY_URL must end in /anthropic")
        return str(url).rstrip("/"), key

    @property
    def email_enabled(self) -> bool:
        # Recipients come from the subscribers table (plus the optional static
        # email_to list), so the channel is enabled on SMTP config alone.
        return bool(self.smtp_host and self.email_from)

    @property
    def bluesky_enabled(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_app_password)


settings = Settings()
city = CityConfig()
