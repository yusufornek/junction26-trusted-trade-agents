import sys
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.api import create_app  # noqa: E402
from app.auth import Credentials  # noqa: E402


def main():
    # Schema generation never starts the gateway or requires deployment credentials.
    app = create_app(keys=Credentials(operator_token="schema-generation-only"))
    target = BACKEND.parent / "contracts/openapi.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))
    print(f"Exported {len(app.openapi()['paths'])} paths to {target}")


if __name__ == "__main__":
    main()
