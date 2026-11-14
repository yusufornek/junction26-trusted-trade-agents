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
        if sep and key.strip() in {"VALIO_API_BASE", "VALIO_API_KEY", "OPENAI_API_KEY", "AGENT_KEYS", "OPERATOR_TOKEN", "OPERATOR_AGENT_ID", "ANCHOR_RPC_URL", "ANCHOR_CHAIN_ID", "ANCHOR_PRIVATE_KEY", "ANCHOR_EXPLORER_TX_URL", "ANCHOR_EVERY_N_ENTRIES"}:
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

    @classmethod
    def from_env(cls):
        load_env()
        return cls(
            anchoring=AnchorConfig.from_env(),
            valio_api_base=os.getenv("VALIO_API_BASE", cls.valio_api_base).rstrip("/"),
            valio_api_key=os.getenv("VALIO_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            keys=Credentials(os.getenv("OPERATOR_TOKEN", ""), os.getenv("AGENT_KEYS", ""),
                             os.getenv("OPERATOR_AGENT_ID", "web-console")),
        )
