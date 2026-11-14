"""Spec 38: published connect kit against isolated sandbox storage and real MCP."""
import asyncio
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import BACKEND
from app.developer_docs import generated, tool_schemas
from app.mcp_server import create_server
from app.models import PolicyUpdateRequest, PolicyDocument
from conftest import agent_headers, raw_product, OPERATOR_HEADERS
from test_mcp_server_card import connected

ROOT = BACKEND.parent
spec = importlib.util.spec_from_file_location("connect_reference", ROOT / "clients/python/reference_buyer.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


@pytest.fixture
def product_rows():
    return [raw_product("1", "Milk crate 1L lactose free", volume=1, multiplier=5),
            raw_product("4", "Milk case 1L lactose free", volume=1, multiplier=10),
            raw_product("3", "Milk egg mix", egg=True)]


@pytest.fixture
def sandbox(gateway):
    gateway.settings.environment = "sandbox"
    return gateway


def test_ac_1_reference_client_full_stock_and_verified_receipt(sandbox, need, upstream):
    async def run():
        small = need.model_copy(update={"quantity": need.quantity.model_copy(update={"value": 10})})
        async with connected(sandbox) as (session, http, _):
            async def jwks():
                return (await http.get("/.well-known/jwks.json")).json()
            order = await reference.run_session(session, "full_stock", jwks, need=small.model_dump(mode="json"))
            assert order["status"] == "CONFIRMED" and order["receipt"]
            assert order["commitment"] == "gateway-simulated"
            context = (await http.get("/v1/me")).json()
            assert context["remaining_budget"]["spent"] == order["total"]
            assert context["remaining_budget"]["remaining"] == 5000 - order["total"]
            # The shared HTTP endpoint must never reuse the first caller's identity.
            refused = await http.post("/mcp", headers={**agent_headers("other"), "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 90, "method": "tools/call",
                      "params": {"name": "get_order_status", "arguments": {"order_id": order["id"]}}})
            assert refused.json()["result"]["structuredContent"]["code"] == "ORDER_NOT_FOUND"
            # Verification is real cryptography, including caller binding and tamper rejection.
            with pytest.raises(Exception):
                reference.verify_receipt({**order, "total": order["total"] + 1}, await jwks(), customer_id="1", agent_id="external")
            with pytest.raises(ValueError, match="scope"):
                reference.verify_receipt(order, await jwks(), customer_id="2", agent_id="external")
    asyncio.run(run())


def test_ac_2_split_stock_waits_and_finishes_after_human_approval(sandbox, need, upstream):
    upstream.model = "Stock"
    upstream.amounts = {"1": 40, "4": 20}
    policy = sandbox.policy.get().model_dump(include=set(PolicyDocument.model_fields))
    policy["agents"]["external"]["autonomy"] = "approval_required"
    sandbox.policy.update(PolicyUpdateRequest(policy=policy, updated_by="Test operator"))
    split_need = need.model_copy(update={"quantity": need.quantity.model_copy(update={"value": 400})})
    waits = []
    async def run():
        async with connected(sandbox) as (session, http, _):
            async def sleep(seconds):
                waits.append(seconds)
                pending = (await http.get("/v1/orders")).json()
                assert len(pending) == 1 and pending[0]["status"] == "PENDING_APPROVAL"
                assert len(pending[0]["lines"]) == 2
                forbidden = await http.post(f'/v1/approvals/{pending[0]["id"]}/approve', json={"approver": "Agent"})
                assert forbidden.status_code == 403
                response = await http.post(f'/v1/approvals/{pending[0]["id"]}/approve',
                    headers=OPERATOR_HEADERS, json={"approver": "Human", "evidence_revision": pending[0]["evidence_revision"]})
                assert response.status_code == 200, response.text
            async def jwks():
                return (await http.get("/.well-known/jwks.json")).json()
            order = await reference.run_session(session, "split_stock", jwks,
                need=split_need.model_dump(mode="json"), sleep=sleep)
            assert waits and all(s > 0 for s in waits)
            assert order["status"] == "CONFIRMED" and order["approver"] == "Human"
    asyncio.run(run())


def test_ac_3_context_scope_limits_environment_and_expired_credential(sandbox, need, upstream):
    with TestClient(create_app(sandbox), headers=agent_headers("external")) as client:
        context = client.get("/v1/me").json()
        assert context["principal"]["id"] == "external" and context["customer_ids"] == ["1"]
        assert context["environment"] == "sandbox"
        assert context["effective_limits"] == {"per_order_eur": 1500, "rolling_24h_eur": 5000}
        assert context["remaining_budget"]["remaining"] == 5000
        assert "place_order" in context["supported_operations"]
        mcp = asyncio.run(create_server(sandbox, "external").call_tool("get_agent_context", {})).structured_content
        for field in ("customer_ids", "mandate", "effective_limits", "remaining_budget", "environment", "supported_operations"):
            assert mcp[field] == context[field]
        # Issued opaque keys expire by issuer removal/rotation, not by unverified JWT claims.
        sandbox.settings.keys = sandbox.settings.keys.__class__("operator", "other:replacement")
        client.app.state.keys = sandbox.settings.keys
        expired = client.get("/v1/me")
        assert expired.status_code == 401
        assert expired.json()["next_action"] == "CONTACT_ADMIN"
        assert "issuer" in expired.json()["detail"] and "/developers#failures" in expired.json()["detail"]
        assert expired.headers["www-authenticate"] == "Bearer"


def test_ac_4_developer_and_llms_pages_and_links_resolve(sandbox):
    with TestClient(create_app(sandbox)) as client:
        page = client.get("/developers")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        llms = client.get("/llms.txt")
        assert llms.status_code == 200 and llms.headers["content-type"].startswith("text/plain")
        urls = re.findall(r'href="([^"#]+)', page.text) + re.findall(r'\]\(([^)]+)\)', llms.text)
        for url in urls:
            assert client.get(html.unescape(url)).status_code == 200, url
        for symptom in ("401", "404", "Unsupported protocol version", "missing customer scope", "Reconnect"):
            assert symptom in page.text
    sandbox.settings.public_url = "https://gateway.example"
    with TestClient(create_app(sandbox), base_url=sandbox.settings.public_url) as client:
        card = client.get("/mcp/server-card").json()
        assert card["remotes"][0]["url"] == "https://gateway.example/mcp"
        response = client.post("/mcp", headers={**agent_headers("external"), "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert response.status_code == 200


def test_ac_5_generated_tables_equal_live_tool_schemas(sandbox, monkeypatch):
    schemas = asyncio.run(tool_schemas(create_server(sandbox)))
    expected = generated(schemas)
    assert (BACKEND / "static/developers.html").read_text() == expected
    assert json.loads((BACKEND / "static/tools.json").read_text()) == schemas
    assert (BACKEND / "static/llms.txt").read_text() == (BACKEND / "docs/llms.txt.in").read_text()
    changed = json.loads(json.dumps(schemas))
    changed["place_order"]["inputSchema"]["properties"]["quote_id"]["description"] = "schema changed"
    assert generated(changed) != expected
    # Configuration snippets resolve to actual installed entry points and transport options.
    source = (BACKEND / "docs/developers.html.in").read_text()
    desktop = json.loads(re.search(r'<pre data-config="claude-desktop">(.*?)</pre>', source, re.S)[1])
    config = desktop["mcpServers"]["valio-aimo"]
    assert config["args"] == ["-m", "app.mcp_server", "--agent-id", "issued-agent"]
    assert '--transport http' in source and '${GATEWAY_URL}/mcp' in source
    # Exercise the installed CLI's real loader without its optional Typer/inspector UI dependency.
    import ast
    import importlib
    import logging
    import sys
    import mcp
    from typing import Any
    from mcp.server import MCPServer, Server
    cli_source = Path(mcp.__file__).parent / "cli/cli.py"
    loader = next(node for node in ast.parse(cli_source.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == "_import_server")
    namespace = {"Path": Path, "sys": sys, "importlib": importlib, "Any": Any, "MCPServer": MCPServer,
                 "LowLevelServer": Server, "logger": logging.getLogger("test-mcp-cli")}
    exec(compile(ast.Module(body=[loader], type_ignores=[]), str(cli_source), "exec"), namespace)
    _import_server = namespace["_import_server"]
    monkeypatch.setattr("app.service.Gateway", lambda: sandbox)
    monkeypatch.setenv("MCP_AGENT_ID", "external")
    assert _import_server(BACKEND / "app/mcp_entry.py", None).agent_id == "external"


def test_ac_6_published_walkthrough_executes_against_in_process_gateway(sandbox, need, upstream, tmp_path):
    """Execute the exact bash/jq/verifier snippet; replace only curl's network transport with IPC/TestClient."""
    assert shutil.which("jq"), "Published REST walkthrough requires jq"
    source = (BACKEND / "static/developers.html").read_text()
    script = html.unescape(re.search(r'<pre data-walkthrough="rest">(.*?)</pre>', source, re.S)[1])
    requests = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="connect-", dir=os.environ.get("TMPDIR", "/tmp")) as scratch:
        bridge_dir = Path(scratch) / "bridge"
        bridge_dir.mkdir()
        stop = threading.Event()
        fake = Path(scratch) / "curl"
        fake.write_text('#!' + str(BACKEND / '.venv/bin/python') + '\n' + '''import os, sys, json, time, uuid
from pathlib import Path
root = Path(os.environ["CONNECT_BRIDGE"])
request = root / str(uuid.uuid4())
request.with_suffix(".tmp").write_text(json.dumps(sys.argv[1:]))
request.with_suffix(".tmp").rename(request.with_suffix(".request"))
response = request.with_suffix(".response")
deadline = time.monotonic() + 15
while not response.exists():
    if time.monotonic() > deadline:
        raise TimeoutError("In-process gateway bridge timed out")
    time.sleep(.005)
status, body = json.loads(response.read_text())
sys.stdout.write(body)
sys.exit(22 if status >= 400 else 0)
''')
        fake.chmod(0o755)
        with TestClient(create_app(sandbox)) as client:
            def bridge():
                try:
                    import time
                    while not stop.is_set():
                        pending = list(bridge_dir.glob("*.request"))
                        if not pending:
                            time.sleep(.005)
                            continue
                        for request_file in pending:
                            args = json.loads(request_file.read_text())
                            url, headers, body = None, {}, None
                            index = 0
                            while index < len(args):
                                arg = args[index]
                                if arg == "-H":
                                    key, value = args[index+1].split(":", 1)
                                    headers[key] = value.strip()
                                    index += 2
                                elif arg == "--data-binary":
                                    body = Path(args[index+1].removeprefix("@")).read_bytes()
                                    index += 2
                                else:
                                    if not arg.startswith("-"):
                                        url = arg
                                    index += 1
                            requests.append(url)
                            response = client.request("POST" if body is not None else "GET", url, headers=headers, content=body)
                            target = request_file.with_suffix(".tmp")
                            target.write_text(json.dumps([response.status_code, response.text]))
                            target.rename(request_file.with_suffix(".response"))
                            request_file.unlink()
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=bridge, daemon=True)
            thread.start()
            env = {**os.environ, "PATH": scratch + os.pathsep + str(BACKEND / ".venv/bin") + os.pathsep + os.environ["PATH"],
                   "CONNECT_BRIDGE": str(bridge_dir), "GATEWAY_URL": "http://testserver", "AGENT_TOKEN": "test-secret-external",
                   "DELIVER_BY": str(need.deliver_by), "TMPDIR": str(tmp_path)}
            result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            stop.set()
            thread.join(5)
            assert not errors
            assert result.returncode == 0, result.stderr
            assert "Receipt verified" in result.stdout
            assert len(requests) == 6
