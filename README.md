# Levisol Supply Chain Planner

A monthly production-sourcing, hub-routing and inventory-norms tool for the Levisol
planning team. Built for planners, not data scientists.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Place `Castrol_Case_comp_data.xlsx` beside `app.py`, or upload any revised workbook
from the sidebar.

## Deploy (Streamlit Community Cloud)

Push this folder to a public GitHub repo, then point Streamlit Cloud at `app.py`.
No secrets or API keys required — the solver runs in-process.

## What it does

| Tab | Output |
|---|---|
| Cost summary | Cash cost split by production, freight, penalties, holding + service by tier |
| Production plan | kl per SKU per plant, respecting the 25 kl batch rule |
| Routing | Plant → hub and hub → CFA volumes and freight |
| Unmet demand | What we chose not to supply, and what it costs |
| Inventory norms | Safety stock, reorder point, days of cover per SKU × CFA |
| Capacity & shadow price | Line utilisation + ₹ value of one more kl of capacity |
| Service–cost frontier | What each point of service costs |
| Compare to baseline | Diff any scenario against a saved plan |

Every input in the sidebar is editable: plant line capacities, production costs,
a freight multiplier, tier fill-rate targets, tier demand multipliers, and the
cost assumptions. Set any line to 0 to simulate an outage.

## Model

A mixed-integer linear program solved with HiGHS (`scipy.optimize.milp`).

**Objective** — minimise total cash cost:
production + plant→hub freight + hub→CFA freight + unmet-demand penalty
+ hub safety-stock shortfall + hub holding cost.

**Constraints**
- Production at each plant is an integer multiple of **25 kl** (batch rule).
- Each plant × product-line capacity is respected. The line is set by pack size:
  ≤1.5LT, 3–5LT, 7–20LT, 50LT, 180–210LT.
- Flow balance at every plant and hub; hub opening stock is used before new production.
- Each CFA's requirement = Jan-26 forecast + safety-stock target − opening inventory.
- Tier fill-rate policy (A 98% / B 97% / C–D 92%) as a soft constraint with a
  tier-escalating breach cost, so scarcity is absorbed by low-tier SKUs first.
- Contractual SKUs carry an escalated penalty and are protected ahead of the rest.

**Inventory norms**
`SS = z·√(L·σ_d² + d̄²·σ_L²)`, `ROP = d̄·L + SS`, `Days of cover = ROP / d̄`.
σ_d is the standard deviation of **forecast error** (actual − forecast), because
replenishment is forecast-driven, so the risk to buffer is what the forecast cannot
predict. σ_L combines production and transit variability. Hub norms are computed on
**pooled** demand, which is why the hub buffer is roughly half the sum of the CFA
buffers it covers.

## It cannot be infeasible

Unmet demand, hub shortfall and policy breach are all **priced decision variables**,
not hard constraints. The model therefore always returns a plan — worst case, one
whose shortfalls carry a stated cost. It will not crash on an extreme input set.
Verified against: total plant outage, capacity cut to zero, demand doubled,
demand halved, cost inversion, and uniform 90–99% service targets.

## Assumptions worth challenging

1. **Tiers are derived, not given.** ABC by cumulative sales volume, per Exhibit F's
   50/30/15/5 volume slabs → 15 A / 34 B / 33 C / 18 D.
2. **Fill-rate targets are read as cycle-service levels.** A true β-service formula
   needs an order quantity Q the case never specifies.
3. **Hub shortfall is priced at 10% of the SKU penalty** (editable). A hub buffer gap
   does not lose a sale today; it raises future stockout risk.
4. **Tier-breach costs are a prioritisation device, not cash.** They are reported
   separately and excluded from the cash total.
5. **Only 6 months of history**, so σ estimates are noisy.
6. **Solved to a ~0.5% optimality gap**, not proven optimal — batch integrality makes
   the proof slow, and 0.5% of ₹139M is below the noise in the input data.
