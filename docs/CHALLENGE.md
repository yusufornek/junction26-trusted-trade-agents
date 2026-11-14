# Challenge — Valio Aimo: Trusted Trade Agents

- **Challenge / partner:** Valio Aimo® (Finnish foodservice wholesaler, part of Valio)
- **Link:** https://hackjunction.app/participate/45/challenges/327 (needs Junction login)
- **In this repo:** Turkish brief `VALIO-AIMO-CHALLENGE-TR.md`, verified API notes `PROJECT.md`,
  data files `data/`
- **Resource portal / mock API:** https://junction26.valioaimo.fi — credentials in `.env.local`
  (see `.env.example`), never in the repo.
- **Prizes:** 1st 4,000 € · 2nd 2,000 € · 3rd 1,000 €
- **Deadlines (Helsinki time, +02:00):**
  - **Preliminary submission: Fri 13.11 23:59** — start a submission on the Junction platform, pick this
    challenge, click *Save Draft*. Placeholder content is fine.
  - Submissions open: Sat 14.11 12:00
  - **Final submission: Sun 15.11 10:00** · review 14:00 · event ends 17:00
- **Submission format (Junction platform form):** project name (≤30 chars)\*, punchline (≤200)\*,
  description (≤5000, markdown)\*, challenge picker\*; optional: live demo URL, source code URL, demo
  video link (YouTube/Loom), other link, presentation file (PDF/PPTX/KEY ≤20 MB), tech challenges
  (any number; say how the tech was used). \* = required.
- **Team:** Gökalp — backend + AI agent (with Claude/Codex) · frontend teammate — UX + frontend (same
  repo) · business teammate — business case and pitch

## Problem statement
How can a foodservice wholesaler let **external, autonomous procurement agents** find products, compare
offers and get the right approval — without exposing real systems, confidential data or the
marketplace roadmap? We prototype the **interface and operating model** an external buyer agent uses
to work with Valio Aimo's (mocked) webshop/backend — built on open, verifiable standards, not one
platform's black box.

Flow: **Request → Lookup → Order → Approval**

> Example: a hotel chain's agent sends *"I need 1,000 litres of lactose-free milk by Monday. Find an
> option that meets our requirements on price and availability."* Our system returns products, prices
> and stock in structured form; the agent either places the order within its authority or routes it to
> a human for sign-off.

### Core (must have)
1. **Lookup** — structured way for an external agent to search products, price and stock.
2. **Request → order** — defined step from query to a commitment at **fixed prices** (no negotiation).
3. **Permissions & approval** — what the agent decides alone vs. what goes to a human to sign.

### Optional (not required to win)
- Delivery reliability + sustainability labels, not just price (category-level CO2 = stretch goal).
- Substitutions and other exceptions (partial stock, "good-better-best" ranking within the range).
- Traceability: human-readable log of what the agent did and why (audit trail for buyer and Valio Aimo).

### Constraints stated in the brief
- We mock today's webshop/backend, **not** the marketplace.
- **Not a chatbot** replacing the e-commerce UI.
- Query APIs only when needed, not for all products.
- Fixed prices; no negotiated quotes.
- Final interface form (REST API vs. MCP-style agent interface vs. service agent) is out of scope — our
  choice, but justify it.

### Problems they want addressed (from "Insight")
- **Trust protocol** — minimum data/verification standard so an agent can trust stock, price and origin claims.
- **Guardrail model** — which decisions need a human (spend threshold, substitutions outside stated
  preferences) and which can be fully autonomous.
- **Response logic** — partial stock and substitution ranking ("good-better-best").
- **Audit trail** — useful, human-readable log for both buyer and Valio Aimo.

Sample scenarios: need → recipe → order; last-minute event; supplier stock-out with recipe-compatible
substitute; proactive Valio Aimo sales agent; allergen-safe substitution (school/hospital); recurring
order drift detection.

Preferred tech: agentic/tool-use frameworks, open data/interop standards, recommendation logic;
optionally verifiable credentials / trust layer. Agentic payments (x402, Nevermined) are context only.

## Judging criteria
| Criterion | Weight | What it means |
|---|---|---|
| Working end-to-end demo | **30%** | Full request → lookup → order/approval in the mock environment, not isolated parts |
| Solving the user need | 20% | How well the agent matches what the buyer actually wants, with little manual correction |
| Autonomy & guardrails | 20% | What the agent decides alone, what needs approval, how decisions are logged and auditable |
| Exception robustness | 15% | Partial stock, substitutions, delivery constraints beyond the happy path |
| Openness & path to production | 15% | Open documented standards, visible assumptions, justified tech choices, credible next steps |

## Mock API
Base `https://junction26.valioaimo.fi`, header `X-API-Key: $VALIO_API_KEY` on everything under `/api`.

| Method | Path | Body |
|---|---|---|
| GET | `/api/ping` | — |
| POST | `/api/availability` | `{customer, requestedDate, products: ["462626", ...]}` |
| POST | `/api/price` | same |
| GET | `/api/products/download` | 307 → file |
| GET | `/api/delivery-reliability/download` | 307 → file |
| GET | `/api/carbon-footprint/download` | 307 → file |

Swagger at `/docs`, `/redoc`, `/openapi.json` (needs portal session: `POST /login`, field `password`).

**Missing on purpose — we build these:** no order/commit endpoint, no search/filter endpoint, no
customer/contract endpoint.

### Verified behaviours / gotchas (observed live by the team)
- `products` must be a **flat string array**; objects → `422`.
- `/api/price` always returns `quantity: 1.0`. `netPriceFound: false` → `catalogPrice` applies.
  `customer` "1" vs "42" gave identical prices.
- **Unit conversion:** `catalogPrice.value / comparisonPrice.value` = litres/kg per sales unit
  (462626: 13.00 € / 2.60 €/L → 5 L per `KI`). Product data has no content-volume field.
- `logisticModel`: `Terminal` → `availableAmount 999999` (made to order, effectively unlimited);
  `Stock` → real finite stock (e.g. 51).
- `timeNow` is the live server clock; compare with `closingTime` (order cut-off, may be null).
- **Silent date shift:** requested 2026-11-14 may come back as `availableDate` 2026-11-16 with HTTP 200 and
  no warning — must compare `requestedDate` vs `scheduleLines[].availableDate` ourselves.
- **Unknown product id** → `scheduleLines: []`, `salesBlock: false`, no error flag.
- Prices carry `validFrom` / `validTo` windows (seen: open-ended `9999-12-31`).
- `salesBlock` = not for sale; `allowedLotSize` = order quantity step (seen 0.01); `error` = per-product flag.

## Data (synthetic, challenge use only)
Files live in `data/` (unpack the catalog with `gunzip -k data/product_data.json.gz`).

| File | Content |
|---|---|
| `product_data.json.gz` (83 MB raw) | 17,546 products, 349 categories, none `deleted` |
| `product_data_schema.json` | JSON Schema |
| `delivery_reliability.csv` | `productId,deliveryReliability` (0–1), ~4.4k products |
| `carbon_footprint.csv` | `productId,AVG kg CO2-eq/kg`, ~11k products — **product-group average**, not per product |

Product fields: `id, salesUnitGtin, salesUnit, baseUnit, category, allowedLotSize, deleted,
temperatureCondition, vendorName, countryOfOrigin, synkkaData, units, deposits`.
Product **name only lives in `synkkaData.names[]`** (fi/sv/en); `vendorName` is the supplier.
Sales units: mostly `KI` (8.0k), `ST` (5.6k), `KG`, `PAK`, `BAG`, `RAS`, `PRK`, `BOT`.
Data quality varies: GTIN packaged goods have rich nutrition/allergen data; fruit & veg mostly don't.
Category `10400` = milk drinks; 62 products have both "milk" and "lactose free" in the name.

Structured attributes inside `synkkaData` (better than matching on names):
- `classifications[]` by `name`: `nutritionalClaim` (7.6k, e.g. `FREE_FROM_LACTOSE`, `FREE_FROM_GLUTEN`,
  `VEGETARIAN`), `allergen` (7.2k, GS1 codes with `CONTAINS`, e.g. `AM` milk, `AE` egg), `nonAllergen`
  (5.1k, `FREE_FROM`), `packagingMarkedLabel` (17k, labels such as `GOODS_FROM_FINLAND_BLUE_SWAN` — usable
  for the sustainability/label comparison), `nutritionFact` / `gda`, `mineral`, `vitamin`.
- `unitConversions[].volume` (LTR) / `netWeight` — pack content for ~9k products, so litres per unit can
  come from the catalog; the `catalogPrice / comparisonPrice` ratio is the fallback.
- `units[]` maps each unit (`ST`, `KI`, `PAL`, ...) to `sizeInBaseUnits` (e.g. 462775: KI = 12 ST, PAL = 576 ST).
- Also: `ingredients`, `keyIngredients`, `storageInstructions`, `brand`, `producers`, min/max temperature.

## Valio Aimo today: A.I.mo (observed 2026-11-13 in My Aimo, logged in)
- A.I.mo is Valio Aimo's own AI assistant for human customers inside My Aimo (minunaimo.valioaimo.fi); My Aimo
  also has purchase reports, carbon footprint of purchases, recipes with margin, customer-service messaging.
- Asked the challenge's sample request ("1,000 L lactose-free milk by Monday … price and availability"), it
  returned three product search lists (online-store product codes) mixing drinking milk with sour/curdled milk and
  2 cl portion packs — no quantity conversion, prices, stock, delivery date, choice or order.
- What our gateway adds on top: structured need → hard filters → litres-to-packs → live price/stock for the
  shortlist → ranked choice → fixed-price quote → order under policy with human sign-off and audit trail.
  Pitch: A.I.mo and outside buyer agents can both use the same governed gateway.

## Mentors
- Marketplace / commercial: Hanna Forsström, Pirjo Kaartinen
- Online sales: Minni Ruhtinas
- IT / integration / data governance: Tommi Nurmi-Aro, Ville Takanen, Riku Tapper

## Open questions
- Webshop test accounts — "shared separately when ready" per the brief.
- `closingTime` already in the past (e.g. 463038: 2026-11-12 15:00) while `availableDate` is still
  2026-11-16 — ask a mentor what the cut-off actually gates.
  Observed 2026-11-13 22:24: for requestedDate 16, 18, 20 and 23.11 the API returned the **same**
  `closingTime` and the same `availableDate` 2026-11-16 for both 463038 (Terminal) and 462775 (Stock) —
  schedule lines do not follow the requested date. Until a mentor confirms otherwise we treat
  `closingTime < timeNow` as "cut-off missed for this delivery" and flag it, never silently order.

## Organiser answers (Junction Discord, 2026-11-14)
- No order API; orders produced for human approval are enough — nothing has to be sent anywhere. A web shop
  account is available at the booth (production, orders cannot be placed).
- `closingTime` is the order cut-off for the related `availableDate`. Mock data stays the same all weekend.
- Availability ≥ 999999 = availability unknown; Valio's logic allows ordering any quantity (Terminal products
  Valio does not stock). Empty availability = not available.
- `allowedLotSize` 0.01 = 1 (fractional crates do not exist).
- Rate limit: 429 blocks for 30 s; a few hundred calls per minute is fine, more than 10 per second is not.
How the gateway follows them: spec 39.
- Approval today: Valio enforces no approval gates; customers handle spending limits with internal
  instructions or by letting only the manager place orders. In an agentic scenario human approval would be
  required at first, possibly dropped later when reliability is good enough. How the gateway follows this:
  spec 40 (approval first, autonomy earned per agent from a measured track record).

## Observations from the production webshop (test account, read only, 2026-11-14)
- Product codes in the webshop differ from the mock's product IDs (mock 464306 = webshop 8043409), but the
  **GTIN/EAN is the same** (6408430000487). GTIN is the bridge between the mock, our UBL export and the real shop.
- Prices differ from the mock (Arki Eila 1 l: webshop 1.43 €/kpl net for the test customer, mock 1.01 €), so the
  mock's prices are illustrative.
- Stock shows as "999+ kpl varastossa" (capped display); per-product "Toimitettavissa 18.11." shows the earliest
  delivery date, matching the availability / date-shift idea.
- Unit conversions are listed per product ("1 ltk = 24 pl", "1 ltk = 20 kpl", "1 pal = 160 kpl") with an EAN per
  level; the comparison price confirms whole-case content (24 × 0.33 L beer: 54.17 €/ltk, 6.84 €/l = 7.92 L per
  case), the same reading the gateway now uses.
- Allergens and nutritional claims are shown per product (e.g. "Allergeenit: Maito"; "Laktoositon").
- Shop features close to ours: order templates ("Lisää tilauspohjalle"), "colleagues often buy these",
  "also bought", product comparison, availability notifications, substitute suggestions. Ordering is add to cart
  with no approval step.
- Case products match the gateway's pack counts: Saarioinen meat balls 5 kg/case (56.00 €/ltk, 11.20 €/kg) and
  4.5 kg/case (59.69 €/ltk, 13.26 €/kg), sold per whole case ("1 ltk = 1 ltk").
- Delivery date is chosen per cart in a calendar (today, selected day, days with a confirmed order, days with an
  open cart); products show their earliest delivery ("Toimitettavissa 18.11."). No order cut-off time is shown on
  product pages or in the calendar; the cut-off lives in the availability data (`closingTime`).

