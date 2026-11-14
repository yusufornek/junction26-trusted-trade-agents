"""Anchor the current audit chain head once; print its public explorer link."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.anchoring import AnchorConfig, AnchorError, Anchoring
from app.config import BACKEND, load_env
from app.db import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=BACKEND / "var/app.db")
    args = parser.parse_args(argv)
    load_env()
    config = AnchorConfig.from_env()
    if not config.enabled:
        print("Missing anchoring variables: " + ", ".join(config.missing), file=sys.stderr)
        return 1
    try:
        anchor = Anchoring(Database(args.db), config).anchor_once(current_head=True)
        print(f"{anchor['status']}: {anchor['explorer_url']}")
        return 0
    except AnchorError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Audit anchoring failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
