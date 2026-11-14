"""Archive local demo state and rebuild the catalog without network calls."""

import argparse
import shutil
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.catalog import build
from app.config import ROOT, Settings
from app.db import Database


@dataclass(frozen=True)
class ResetResult:
    archive_dir: Path
    products: int


def api_running(port):
    """Treat an answering local API port as running, without changing its state."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def reset_demo(db_path, runtime_policy_path, archive_root, source, reliability_path, carbon_path,
               *, port=8000, force=False, port_check=None):
    """Move the complete previous state aside, then create a fresh catalog database."""
    if (port_check or api_running)(port) and not force:
        raise RuntimeError(f"The API port {port} is answering. Stop the API first, then retry (or use --force).")

    db_path, runtime_policy_path, archive_root = map(Path, (db_path, runtime_policy_path, archive_root))
    archive_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    while True:
        archive_dir = archive_root / timestamp.strftime("%Y%m%d-%H%M%S")
        try:
            archive_dir.mkdir()
            break
        except FileExistsError:
            # Preserve the timestamp format and every archive, including resets in the same second.
            timestamp += timedelta(seconds=1)

    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), runtime_policy_path):
        if path.exists():
            shutil.move(str(path), archive_dir / path.name)

    result = build(Database(db_path), source, reliability_path, carbon_path)
    # With no runtime policy copy, Policy.get() falls back to the committed defaults.
    return ResetResult(archive_dir=archive_dir, products=result["rows"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, help="Gateway API port (default: 8000).")
    parser.add_argument("--force", action="store_true", help="Continue even if the API port is answering.")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        result = reset_demo(
            settings.db_path, settings.db_path.parent / "policy.yaml", settings.db_path.parent / "archive",
            ROOT / "data/product_data.json.gz", ROOT / "data/delivery_reliability.csv",
            ROOT / "data/carbon_footprint.csv", port=args.port, force=args.force,
        )
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Archived previous state: {result.archive_dir}")
    print(f"Catalog products: {result.products}")
    print(f"Start API: .venv/bin/python -m uvicorn app.api:app --port {args.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
