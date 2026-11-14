# 21 — Public anchoring of the audit chain (Polygon)

Status: in progress · Decided on 2026-11-14 · Builds on [11 — Tamper-evident audit](11-tamper-evident-audit.md).

## Purpose
The audit chain proves integrity against checkpoints kept outside the gateway. A public blockchain is a
checkpoint nobody controls and everybody can read: the gateway writes the chain's head hash into a Polygon
transaction; anyone can later compare the chain against that transaction on a block explorer.

## Requirements
- **R-1 What is written:** only `{entries, head_hash}` of the audit chain, as transaction data with a fixed
  prefix (`VAIMO-AUDIT-1:` + entries + `:` + head hash hex). No business data leaves the gateway.
- **R-2 How:** a self-addressed EIP-1559 transaction with zero value on an EVM chain, signed locally with the
  anchoring key, sent through standard Ethereum JSON-RPC (`eth_chainId`, `eth_getTransactionCount`,
  `eth_estimateGas`, `eth_feeHistory` / `eth_maxPriorityFeePerGas`, `eth_sendRawTransaction`,
  `eth_getTransactionReceipt`, `eth_getTransactionByHash`). No smart contract.
- **R-3 Configuration:** `ANCHOR_RPC_URL`, `ANCHOR_CHAIN_ID` (137 = Polygon mainnet, 80002 = Amoy testnet),
  `ANCHOR_PRIVATE_KEY`, `ANCHOR_EXPLORER_TX_URL` (e.g. `https://polygonscan.com/tx/`) from the environment /
  `.env.local`, documented in `.env.example` by name only. Without them anchoring is off and the rest of the
  gateway works unchanged.
- **R-4 Triggers:** `backend/scripts/anchor_audit.py` anchors the current head once and prints the explorer
  link; with `ANCHOR_EVERY_N_ENTRIES` set, the gateway anchors automatically in the background after every N
  new entries (never inside a request's database transaction; a failed anchor is retried on the next trigger and
  never blocks business calls).
- **R-5 Record:** table `audit_anchors` `{entries, head_hash, chain_id, tx_hash, block_number, status
  (sent | confirmed | failed), explorer_url, at}`; an `AUDIT_ANCHORED` audit entry per confirmed anchor;
  `GET /v1/audit/anchors` lists them.
- **R-6 Verify against the chain:** `GET /v1/audit/verify?anchor=latest|<tx_hash>` reads the transaction
  through the RPC, checks prefix, entries and head hash against the recomputed chain, and reports
  `anchor_match` and the block time.
- **R-7 Key safety:** the private key appears in no response, log line, audit entry or exception text; the
  anchoring address (public) is shown in `GET /health`'s anchoring section.

## Acceptance criteria
- **AC-1** With a mocked JSON-RPC, `anchor_audit.py` builds a correctly signed EIP-1559 transaction (chain id,
  nonce, zero value, to = own address, data = R-1 payload) and records `sent` then `confirmed` from the receipt.
- **AC-2** `verify?anchor=latest` reports `anchor_match: true` for an untouched chain and `false` after a row
  before the anchored entry was edited.
- **AC-3** Without configuration: no RPC call is made, `GET /v1/audit/anchors` returns an empty list,
  `anchor_audit.py` exits 1 with a message naming the missing variables.
- **AC-4** RPC failure during automatic anchoring: business calls succeed, the anchor row is `failed`, the next
  trigger retries.
- **AC-5** The private key value appears nowhere in responses, logs or the audit table (scan in the test).
- **AC-6** Against a local EVM node (anvil) when available: a real transaction is sent and verified end to end
  (test skipped when anvil is not installed).

## For the review
Mainnet anchoring is funded and run by Gökalp from his own wallet; the code is tested with mocked RPC and a
local node. Cost per anchor on Polygon PoS is a fraction of a cent.
