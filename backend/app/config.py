import os
from dataclasses import dataclass, field
from pathlib import Path

from .auth import Credentials
from .anchoring import AnchorConfig

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def load_env(path: Path = ROOT / ".env.local") -> None:
    """Load simple KEY=value lines without overriding the process environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"DB_PATH", "SIGNING_KEY_DIR", "VALIO_API_BASE", "VALIO_API_KEY", "OPENAI_API_KEY", "AGENT_KEYS", "OPERATOR_TOKEN", "OPERATOR_AGENT_ID", "ANCHOR_RPC_URL", "ANCHOR_CHAIN_ID", "ANCHOR_PRIVATE_KEY", "ANCHOR_EXPLORER_TX_URL", "ANCHOR_EVERY_N_ENTRIES", "WEBHOOK_SANDBOX", "GATEWAY_ENVIRONMENT", "GATEWAY_PUBLIC_URL", "MCP_AGENT_ID", "NORDFOOD_API_BASE", "SANDBOX_DB_PATH", "SANDBOX_AGENT_KEYS", "SANDBOX_ADMIN_TOKEN", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_CONSOLE"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            os.environ.setdefault(key.strip(), value)


@dataclass
class Settings:
    db_path: Path = BACKEND / "var/app.db"
    policy_path: Path = BACKEND / "policy.yaml"
    valio_api_base: str = "https://junction26.valioaimo.fi"
    valio_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    keys: Credentials | None = field(default=None, repr=False)
    anchoring: AnchorConfig = field(default_factory=AnchorConfig, repr=False)
    signing_key_dir: Path | None = None  # Defaults to db_path.parent / keys; fixtures stay isolated.
    environment: str = "live"  # A deployment and its credentials/storage belong to one environment.
    public_url: str = ""  # Explicit public origin allowed by MCP DNS rebinding protection.
    webhook_sandbox: bool = False  # Only this explicit sandbox permits http://localhost destinations.
    nordfood_api_base: str = "http://127.0.0.1:8002"

    def __post_init__(self):
        if self.environment not in {"live", "sandbox"}:
            raise ValueError("GATEWAY_ENVIRONMENT must be live or sandbox")
        if self.public_url:
            from urllib.parse import urlsplit
            url = urlsplit(self.public_url)
            if url.scheme not in {"http", "https"} or not url.netloc or url.path not in {"", "/"} or url.query or url.fragment or url.username:
                raise ValueError("GATEWAY_PUBLIC_URL must be an HTTP(S) origin")
            self.public_url = self.public_url.rstrip("/")

    @classmethod
    def from_env(cls):
        load_env()
        environment = os.getenv("GATEWAY_ENVIRONMENT") or "live"
        sandbox = environment == "sandbox"
        return cls(
            environment=environment,
            public_url=os.getenv("GATEWAY_PUBLIC_URL", ""),
            db_path=Path((os.getenv("SANDBOX_DB_PATH") or str(BACKEND / "sandbox-state/app.db")) if sandbox else (os.getenv("DB_PATH") or cls.db_path)),
            webhook_sandbox=os.getenv("WEBHOOK_SANDBOX", "").lower() == "true",
            signing_key_dir=Path(os.environ["SIGNING_KEY_DIR"]) if os.getenv("SIGNING_KEY_DIR") and not sandbox else None,
            anchoring=AnchorConfig() if sandbox else AnchorConfig.from_env(),
            nordfood_api_base=os.getenv("NORDFOOD_API_BASE", cls.nordfood_api_base),
            valio_api_base=os.getenv("VALIO_API_BASE", cls.valio_api_base).rstrip("/"),
            valio_api_key=os.getenv("VALIO_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            keys=Credentials(os.getenv("SANDBOX_ADMIN_TOKEN" if sandbox else "OPERATOR_TOKEN", ""),
                             os.getenv("SANDBOX_AGENT_KEYS" if sandbox else "AGENT_KEYS", ""),
                             os.getenv("OPERATOR_AGENT_ID", "web-console"), environment=environment),
        )
