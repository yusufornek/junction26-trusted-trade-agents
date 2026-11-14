"""Append missing operator/agent credentials without displaying their values."""
import argparse
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND, ROOT
from app.policy import Policy


def provision(env_file, agent_ids):
    content = env_file.read_text() if env_file.exists() else ""
    lines = content.splitlines(keepends=True)
    entries = {}
    for index, line in enumerate(lines):
        text = line.strip().removeprefix("export ")
        name, sep, value = text.partition("=")
        if sep and name.strip() in {"AGENT_KEYS", "OPERATOR_TOKEN"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            if name.strip() in entries:
                raise ValueError("Duplicate credential variable; keep one definition per name")
            entries[name.strip()] = (index, value)
    added, changes = [], {}
    current = entries.get("AGENT_KEYS", (None, ""))[1]
    pairs = [pair.strip() for pair in current.split(",") if pair.strip()]
    identities = {}
    for pair in pairs:
        agent_id, sep, secret = pair.partition(":")
        if not sep or not agent_id or agent_id in identities:
            raise ValueError("Invalid AGENT_KEYS; expected distinct agent-id:secret pairs")
        identities[agent_id] = secret
    for agent_id in sorted(agent_ids):
        if not identities.get(agent_id):
            pairs = [pair for pair in pairs if pair.partition(":")[0] != agent_id]
            pairs.append(f"{agent_id}:{secrets.token_urlsafe(32)}")
            added.append(f"AGENT_KEYS ({agent_id})")
    if any(name.startswith("AGENT_KEYS") for name in added):
        changes["AGENT_KEYS"] = ",".join(pairs)
    if not entries.get("OPERATOR_TOKEN", (None, ""))[1]:
        changes["OPERATOR_TOKEN"] = secrets.token_urlsafe(32)
        added.append("OPERATOR_TOKEN")
    if changes:
        for name, value in changes.items():
            entry = entries.get(name)
            if entry:
                lines[entry[0]] = f"{name}={value}\n"
            else:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(f"{name}={value}\n")
        # Credentials are local files readable by the owner only, including newly created files.
        with open(env_file, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            file.write("".join(lines))
        env_file.chmod(0o600)
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", required=True, help="Write missing credentials")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    args = parser.parse_args()
    policy = Policy(BACKEND / "policy.yaml", BACKEND / "var/policy.yaml").get()
    try:
        added = provision(args.env_file, policy.agents)
    except ValueError as error:
        parser.error(str(error))
    print("Added: " + ", ".join(added) if added else "No names added.")


if __name__ == "__main__":
    main()
