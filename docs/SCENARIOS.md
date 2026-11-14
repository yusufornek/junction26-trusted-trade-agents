# The five brief scenarios

Run commands from `backend/`, using your installed backend Python environment (`.venv/bin/python` below).
Each script is a real MCP client against an in-process gateway with its own named sandbox fixture. All state,
keys and evidence reports live in temporary directories; the live demo database is untouched. Supplier dates
are deterministic offsets from the run date. Each run prints its steps, the human decision, and a JSON evidence path.
Procurement uses MCP; the simulated human uses the existing buyer approval service, since human approval is not an agent tool.

## 1. Need → recipe → order

**Agent says:** “Make the seasonal lunch for ten. The sauce needs 10 L of oat drink; we already have 7 L.”

**Gateway does:** Scales the manifest, subtracts physical inventory, converts the remaining 3 L into whole packs,
keeps the focus ingredient first in coverage, and creates one basket quote and order. No recipe is invented by
the gateway: the buyer's agent supplies its ingredients and functional use.

**Human decides:** The menu, recipe and inventory. The established buyer agent may order the unchanged preferred
ingredient within its authority; a replacement for the focus ingredient always needs approval.

**Command:** `.venv/bin/python demo/scenario_1_recipe_order.py`

## 2. Last-minute event

**Agent says:** “We need the same sauce for tomorrow's event.”

**Gateway does:** Compares the supplier's delivery date and order cut-off with the deadline. The usual drink is
three days away; the cooking-compatible alternative can arrive tomorrow. The late line remains visible with
`cannot_source_in_time`, the earliest supplier date and an in-time alternative. One shared trade-off calculation
shows required-quantity price difference, pack/unit changes, allergen/claim differences and delivery difference.

**Human decides:** Whether to approve the replacement for the focus ingredient. The sandbox buyer approves it;
the agent polls the same order and retrieves its signed receipt.

**Command:** `.venv/bin/python demo/scenario_2_last_minute_event.py`

## 3. Supplier stock-out

**Agent says:** “Watch our approved recurring kitchen purchase before it is due in three days.”

**Gateway does:** Checks the next seven days of active recurring occurrences and open baskets/quotes. After the
supplier stock changes, it emits `STOCKOUT_PREDICTED`, records a buyer inbox entry, and delivers a signed
`stockout.predicted` webhook to an in-memory receiver that verifies the signature. The substitute passes the
same function and constraints as normal lookup; the watch never changes or places an order. Repeated scans do
not duplicate the same source/product/date alert. The existing scheduler scans at most once per minute;
`POST /v1/stockout-watch/run` triggers an explicit scan.

**Human decides:** Whether the proposed substitute and its trade-offs are acceptable. The agent prepares a normal
replacement quote/order; the sandbox buyer approves it and the agent retrieves the receipt.

**Command:** `.venv/bin/python demo/scenario_3_stockout.py`

## 4. Valio Aimo's proactive sales agent

**Agent says:** “Suggest new products that fit what this consenting buyer actually purchases.”

**Gateway does:** Reads its private purchase memory, finds never-bought products in purchased categories, applies
allergen and nutrition constraints, gets current price evidence, and submits through the existing campaign
service. Buyer consent, weekly cadence, duplicate campaigns and supplier/customer quotas still apply. The
supplier receives campaign totals rather than purchase history or buyer limits. Acceptance uses normal
lookup → quote → order with live evidence revalidation.

**Human decides:** Whether to opt in to campaigns and which categories/channels are welcome. Acceptance remains
under the buyer agent's ordinary mandate and approval rules.

**Command:** `.venv/bin/python demo/scenario_4_proactive_selling.py`

## 5. Allergen-safe substitution under pressure

**Agent says:** “Our hospital's usual cooking drink is unavailable. Find a suitable replacement under the
recorded milk-allergen exclusion and maximum salt rule.”

**Gateway does:** Removes the milk-containing blend, the high-salt product, the product without nutrition facts,
and the similar-looking dessert that fails the functional need. It presents the compliant cooking drink with
its evidence and trade-off, and holds the order for the hospital buyer's review. Catalog nutrient values are
normalised from explicit quantity/basis units to grams per 100 g or 100 ml; missing facts never prove compliance.

**Human decides:** The hospital buyer approver performs the required clinical review and approves the substitute.
“Compliant with the recorded procurement constraints” is a procurement finding, not a clinical safety claim.
The agent then polls and retrieves its receipt.

**Command:** `.venv/bin/python demo/scenario_5_allergen_safe_substitution.py`
