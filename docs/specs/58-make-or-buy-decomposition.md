# 58 — Make-or-buy decomposition

Status: in progress · Decided on 2026-11-14 with Gökalp from the business teammate's suggestion 5 (capability spec,
see spec 47). Builds on specs 31 (functional substitution), 32 (recipe to basket), 46 R-3 (trade-off) and 49
(constraint engine).

## Purpose
When a prepared component is missing, late or unsuitable — béchamel for lasagne, stock, pie dough, dressing, a sauce
base — the gateway offers to make it from its ingredients as one alternative next to ready-made substitutes, with the
real trade-off, and the reverse: buying a ready component when the kitchen lacks time or skill.

## Requirements
- **R-1 Component knowledge:** a versioned component library (`backend/components/*.yaml`) describes prepared
  components: name and synonyms (fi/en/sv), yield, ingredients with quantities per yield and their functional needs
  (spec 31), preparation time, skill level, equipment and allergens it introduces. Seed at least ten common
  foodservice components.
- **R-2 Recognition:** a lookup or basket line whose product type matches a component (by catalog category, synonyms or
  functional need) is marked `decomposable`.
- **R-3 Make alternative:** when the ready product is unavailable, late (spec 46 R-2), excluded by constraints or more
  expensive beyond a threshold, the gateway adds a `make` alternative: scaled ingredient lines (resolved to offers by
  the normal lookup and the constraint engine), minus on-hand stock (spec 46 R-1), with a `trade_off`: ingredient cost
  vs ready cost, labour minutes, skill, equipment, allergen differences and delivery feasibility of every ingredient.
- **R-4 Buy alternative:** a recipe basket (spec 32) whose kitchen profile says no time or skill for a step can be
  offered the ready component instead, with the same trade-off.
- **R-5 Constraints first:** every ingredient passes the customer's constraints; if one cannot, the make alternative is
  shown as not possible with the reason.
- **R-6 Human decision:** make or buy is always the kitchen's choice; accepting adds the lines to the basket as one
  group linked to the original line.
- **R-7 Surfaces:** REST and MCP (`decompose_component`, alternatives inside lookup and basket responses).

## Acceptance criteria
- **AC-1** Lasagne for 40 with no béchamel available returns a make alternative (butter, flour, milk scaled to 40
  portions) with cost, labour minutes and allergens next to any ready substitute.
- **AC-2** On-hand butter reduces the make basket.
- **AC-3** For a lactose-free customer the make alternative uses lactose-free milk or is shown as not possible with
  the reason.
- **AC-4** A recipe basket for a kitchen without time for stock offers ready stock with the trade-off.
- **AC-5** Adding a new component is a library entry only; it works in lookup without code changes.
