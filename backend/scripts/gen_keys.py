"""Append missing operator/agent credentials without displaying their values."""
import argparse
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BACKEND, ROOT
from app.policy import Policy


def provision(env_file, agent_ids, *, sandbox=False):
    from app.accounts import DEMO_USERS
    password_vars = {row[-1] for row in DEMO_USERS} if not sandbox else set()
    agent_var = "SANDBOX_AGENT_KEYS" if sandbox else "AGENT_KEYS"
    operator_var = "SANDBOX_ADMIN_TOKEN" if sandbox else "OPERATOR_TOKEN"
    content = env_file.read_text() if env_file.exists() else ""
    lines = content.splitlines(keepends=True)
    entries = {}
    for index, line in enumerate(lines):
        text = line.strip().removeprefix("export ")
        name, sep, value = text.partition("=")
        if sep and name.strip() in {agent_var, operator_var} | password_vars:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            if name.strip() in entries:
                raise ValueError("Duplicate credential variable; keep one definition per name")
            entries[name.strip()] = (index, value)
    added, changes = [], {}
    current = entries.get(agent_var, (None, ""))[1]
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
            added.append(f"{agent_var} ({agent_id})")
    if any(name.startswith(agent_var) for name in added):
        changes[agent_var] = ",".join(pairs)
    if not entries.get(operator_var, (None, ""))[1]:
        changes[operator_var] = secrets.token_urlsafe(32)
        added.append(operator_var)
    if not sandbox:
        for *_, variable in DEMO_USERS:
            if not entries.get(variable, (None, ""))[1]:
                changes[variable] = secrets.token_urlsafe(24)
                added.append(variable)
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
    parser.add_argument("--write", action="store_true", help="Write missing live credentials")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument("--sandbox-agent", help="Issue a sandbox-only agent credential (and sandbox admin token)")
    args = parser.parse_args()
    if not args.write and not args.sandbox_agent:
        parser.error("Use --write or --sandbox-agent ID")
    policy = Policy(BACKEND / "policy.yaml", BACKEND / "policy.yaml").get()
    try:
        added = provision(args.env_file, {args.sandbox_agent} if args.sandbox_agent else set(policy.agents) | set(policy.identities), sandbox=bool(args.sandbox_agent))
    except ValueError as error:
        parser.error(str(error))
    print("Added: " + ", ".join(added) if added else "No names added.")


if __name__ == "__main__":
    main()
