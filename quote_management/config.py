import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    base_url: str = field(default_factory=lambda: os.getenv("TMFORUM_API_BASE_URL", "http://localhost:8081/tmf-api").strip().rstrip("/"))
    party_url: str = field(default_factory=lambda: os.getenv("TMFORUM_PARTY_API_BASE_URL", "").strip().rstrip("/"))
    catalog_url: str = field(default_factory=lambda: os.getenv("TMFORUM_PRODUCT_CATALOG_API_BASE_URL", "").strip().rstrip("/"))
    document_url: str = field(default_factory=lambda: os.getenv("TMFORUM_DOCUMENT_API_BASE_URL", "").strip().rstrip("/"))
    notification_url: str = field(default_factory=lambda: os.getenv("NOTIFICATION_API_BASE_URL", "").rstrip("/"))
    did: str = field(default_factory=lambda: os.getenv("DID_IDENTIFIER", "did:elsi:VATSB-12345678J"))
    token: str = field(default_factory=lambda: os.getenv("TMFORUM_API_TOKEN", ""))
    page_size: int = field(default_factory=lambda: int(os.getenv("QUOTE_PAGINATION_PAGE_SIZE", "5")))
    timeout: float = field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")))
    verify_attempts: int = field(default_factory=lambda: int(os.getenv("ATTACHMENT_VERIFICATION_MAX_ATTEMPTS", "5")))
    verify_delay: float = field(default_factory=lambda: float(os.getenv("ATTACHMENT_VERIFICATION_DELAY_MS", "1000")) / 1000)
    verify_attachment: bool = field(default_factory=lambda: os.getenv("ATTACHMENT_VERIFICATION_ENABLED", "true").lower() == "true")
    scheduler_enabled: bool = field(default_factory=lambda: os.getenv("SCHEDULER_ENABLED", "true").lower() == "true")
    cors_origins: list[str] = field(default_factory=lambda: os.getenv("CORS_ORIGINS", "http://localhost:8081").split(","))

    def __post_init__(self):
        if self.page_size < 1 or self.timeout <= 0 or self.verify_attempts < 1 or self.verify_delay < 0:
            raise ValueError("Invalid pagination, timeout, or attachment verification settings")
        self.party_url = self.party_url or self.base_url
        self.catalog_url = self.catalog_url or self.base_url
        self.document_url = self.document_url or self.base_url
