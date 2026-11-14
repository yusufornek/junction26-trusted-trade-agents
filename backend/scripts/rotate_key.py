#!/usr/bin/env python3
"""Rotate the gateway Ed25519 key; retain the previous public key for seven days."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import BACKEND  # noqa: E402
from app.signing import SigningKeys  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", type=Path, default=BACKEND / "var/keys")
    args = parser.parse_args()
    print("Active signing kid:", SigningKeys(args.keys_dir).rotate())


if __name__ == "__main__":
    main()
