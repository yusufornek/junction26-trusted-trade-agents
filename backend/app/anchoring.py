"""Public audit checkpoints via locally signed EIP-1559 JSON-RPC transactions."""
import fcntl
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from eth_account import Account

from . import audit

REQUIRED = ("ANCHOR_RPC_URL", "ANCHOR_CHAIN_ID", "ANCHOR_PRIVATE_KEY", "ANCHOR_EXPLORER_TX_URL")
PREFIX = "VAIMO-AUDIT-1:"


class AnchorError(Exception):
    """Only constant, sanitized messages cross this boundary."""


@dataclass
class AnchorConfig:
    rpc_url: str = field(default="", repr=False)
    chain_id: str = ""
    private_key: str = field(default="", repr=False)
    explorer_tx_url: str = ""
    every_n_entries: str = ""

    @classmethod
    def from_env(cls):
        import os
        return cls(*(os.getenv(name, "") for name in (*REQUIRED, "ANCHOR_EVERY_N_ENTRIES")))

    @property
    def missing(self):
        return [name for name, value in zip(REQUIRED, (self.rpc_url, self.chain_id, self.private_key, self.explorer_tx_url)) if not value]

    @property
    def enabled(self):
        return not self.missing

    @property
    def every(self):
        try:
            return max(0, int(self.every_n_entries or 0))
        except (ValueError, TypeError):
            return 0


class Anchoring:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self._guard = threading.Lock()
        self._worker = None
        self._closed = False
        self._last_attempt = 0

    def health(self):
        if not self.config.enabled:
            return None
        try:
            address = Account.from_key(self.config.private_key).address
            chain = int(self.config.chain_id)
            return {"enabled": True, "address": address, "chain_id": chain}
        except Exception:
            return {"enabled": False, "error": "Invalid anchoring configuration"}

    def _rpc(self, client, method, params):
        try:
            response = client.post(self.config.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            response.raise_for_status()
            body = response.json()
            if body.get("error") or "result" not in body:
                raise ValueError()
            return body["result"]
        except Exception:
            raise AnchorError("Anchoring RPC failed") from None

    def list(self):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT entries,head_hash,chain_id,tx_hash,block_number,status,explorer_url,at "
                                                     "FROM audit_anchors ORDER BY id DESC")]

    def _confirm(self, client, anchor_id, tx_hash):
        receipt = self._rpc(client, "eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return
        if receipt.get("transactionHash", "").lower() != tx_hash.lower():
            raise AnchorError("Anchoring receipt mismatch")
        block = int(receipt["blockNumber"], 16)
        success = int(receipt["status"], 16) == 1
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM audit_anchors WHERE id=?", (anchor_id,)).fetchone()
            conn.execute("UPDATE audit_anchors SET status=?,block_number=? WHERE id=?",
                         ("confirmed" if success else "failed", block, anchor_id))
            if success and row["status"] != "confirmed":
                audit.append(conn, "system", "AUDIT_ANCHORED", "Audit checkpoint confirmed on chain.",
                             data={key: row[key] for key in ("entries", "head_hash", "chain_id", "tx_hash", "explorer_url")})

    def anchor_once(self):
        if not self.config.enabled:
            raise AnchorError("Missing anchoring variables: " + ", ".join(self.config.missing))
        anchor_id = None
        try:
            # Serialize CLI and worker attempts across processes without a database transaction over RPC.
            with (self.db.path.parent / ".anchor.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with httpx.Client(timeout=10, trust_env=False) as client:
                    chain_id = int(self.config.chain_id)
                    with self.db.connect() as conn:
                        pending = conn.execute("SELECT id,tx_hash FROM audit_anchors WHERE tx_hash IS NOT NULL AND block_number IS NULL AND status IN ('sent','failed') ORDER BY id LIMIT 1").fetchone()
                    if pending:
                        anchor_id = pending["id"]
                        if int(self._rpc(client, "eth_chainId", []), 16) != chain_id:
                            raise AnchorError("Anchoring chain ID mismatch")
                        self._confirm(client, anchor_id, pending["tx_hash"])
                    else:
                        checkpoint = audit.verify(self.db)
                        if not checkpoint["valid"]:
                            raise AnchorError("Audit chain is invalid")
                        with self.db.connect(write=True) as conn:
                            cursor = conn.execute("INSERT INTO audit_anchors(entries,head_hash,chain_id,status,at) VALUES(?,?,?,'sent',?)",
                                (checkpoint["entries"], checkpoint["head_hash"], chain_id, audit.utcnow().isoformat()))
                            anchor_id = cursor.lastrowid
                        if int(self._rpc(client, "eth_chainId", []), 16) != chain_id:
                            raise AnchorError("Anchoring chain ID mismatch")
                        account = Account.from_key(self.config.private_key)
                        nonce = int(self._rpc(client, "eth_getTransactionCount", [account.address, "pending"]), 16)
                        priority = int(self._rpc(client, "eth_maxPriorityFeePerGas", []), 16)
                        history = self._rpc(client, "eth_feeHistory", ["0x1", "latest", []])
                        base_fee = int(history["baseFeePerGas"][-1], 16)
                        payload = f"{PREFIX}{checkpoint['entries']}:{checkpoint['head_hash']}"
                        tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "to": account.address, "value": 0,
                              "data": "0x" + payload.encode("ascii").hex(), "maxPriorityFeePerGas": priority,
                              "maxFeePerGas": 2 * base_fee + priority}
                        tx["gas"] = int(self._rpc(client, "eth_estimateGas", [{**{k: hex(v) if isinstance(v, int) else v for k, v in tx.items()},
                                                                             "from": account.address}]), 16)
                        signed = account.sign_transaction(tx)
                        expected_hash = "0x" + signed.hash.hex().removeprefix("0x")
                        tx_hash = self._rpc(client, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
                        if not isinstance(tx_hash, str) or tx_hash.lower() != expected_hash.lower():
                            raise AnchorError("Anchoring transaction hash mismatch")
                        explorer_url = self.config.explorer_tx_url.rstrip("/") + "/" + tx_hash
                        with self.db.connect(write=True) as conn:
                            conn.execute("UPDATE audit_anchors SET tx_hash=?,explorer_url=? WHERE id=?", (tx_hash, explorer_url, anchor_id))
                        self._confirm(client, anchor_id, tx_hash)
                return next(row for row in self.list() if row["tx_hash"] == (pending["tx_hash"] if pending else tx_hash))
        except Exception:
            if anchor_id is not None:
                with self.db.connect(write=True) as conn:
                    # Keep the hash for receipt retries without duplicate submission.
                    conn.execute("UPDATE audit_anchors SET status='failed' WHERE id=?", (anchor_id,))
            raise AnchorError("Audit anchoring failed; retry on the next trigger") from None

    def verify(self, selector):
        result = {"anchor_match": False, "anchor_block_time": None}
        if not self.config.enabled:
            return result
        try:
            if selector == "latest":
                with self.db.connect() as conn:
                    row = conn.execute("SELECT tx_hash FROM audit_anchors WHERE status='confirmed' ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:
                    return result
                selector = row[0]
            with httpx.Client(timeout=10, trust_env=False) as client:
                if int(self._rpc(client, "eth_chainId", []), 16) != int(self.config.chain_id):
                    return result
                tx = self._rpc(client, "eth_getTransactionByHash", [selector])
                receipt = self._rpc(client, "eth_getTransactionReceipt", [selector])
                if not tx or not receipt or int(receipt["status"], 16) != 1:
                    return result
                if (tx["hash"].lower() != selector.lower() or receipt["transactionHash"].lower() != selector.lower() or
                        tx["blockNumber"] != receipt["blockNumber"] or tx["from"].lower() != tx["to"].lower() or
                        int(tx["value"], 16) != 0):
                    return result
                payload = bytes.fromhex(tx["input"].removeprefix("0x")).decode("ascii")
                prefix, entries, digest = payload.split(":")
                if prefix + ":" != PREFIX or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    return result
                checkpoint = audit.verify(self.db, int(entries), digest)
                block = self._rpc(client, "eth_getBlockByNumber", [tx["blockNumber"], False])
                result["anchor_match"] = checkpoint["checkpoint_match"]
                result["anchor_block_time"] = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
        except Exception:
            pass  # Unavailable or malformed chain evidence is never a positive verification.
        return result

    def trigger(self):
        if not self.config.enabled or not self.config.every or self._closed or threading.current_thread() is self._worker:
            return
        with self._guard:
            if self._worker and self._worker.is_alive():
                return
            with self.db.connect() as conn:
                count = conn.execute("SELECT count(*) FROM audit WHERE action!='AUDIT_ANCHORED'").fetchone()[0]
                latest = conn.execute("SELECT entries FROM audit_anchors WHERE status='confirmed' ORDER BY id DESC LIMIT 1").fetchone()
                baseline = conn.execute("SELECT count(*) FROM (SELECT action FROM audit ORDER BY id LIMIT ?) WHERE action!='AUDIT_ANCHORED'",
                                        (latest[0],)).fetchone()[0] if latest else 0
            if count <= self._last_attempt or count - baseline < self.config.every:
                return
            self._last_attempt = count
            def run():
                try:
                    self.anchor_once()
                except AnchorError:
                    pass
            self._worker = threading.Thread(target=run, name="audit-anchor", daemon=True)
            self._worker.start()

    def close(self):
        self._closed = True
        if self._worker:
            self._worker.join(timeout=15)
