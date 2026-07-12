"""
LEVISOL SUPPLY CHAIN PLANNER
Monthly production & distribution planning tool.
Run:  streamlit run app.py
"""
import io
import os
import time
import copy
import numpy as np
import pandas as pd
import streamlit as st

import engine as E

st.set_page_config(page_title="Levisol Supply Chain Planner",
                   page_icon="🛢️", layout="wide")

CSS = """
<style>
  .stApp { background:#FBFCFD; }
  h1,h2,h3 { color:#0B2A3B; font-family:'Segoe UI',Arial,sans-serif; }
  .kpi { background:#FFFFFF; border:1px solid #E3E8EE; border-left:4px solid #00843D;
         border-radius:6px; padding:14px 16px; }
  .kpi .lab { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#6B7A87; }
  .kpi .val { font-size:26px; font-weight:700; color:#0B2A3B; line-height:1.2; }
  .kpi .sub { font-size:11px; color:#8A97A3; }
  .warn { border-left-color:#D4351C !important; }
  .good { border-left-color:#00843D !important; }
  .banner { background:#EAF4EC; border:1px solid #BFDCC7; border-radius:6px;
            padding:10px 14px; color:#12492B; font-size:13px; }
  .banner-red { background:#FDECEA; border-color:#F3C0BA; color:#7A1C11; }
  div[data-testid="stMetricValue"] { font-size:22px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def inr(x):
    """Indian money convention: crores >= 1Cr, lakhs >= 1L, else plain rupees."""
    x = float(x)
    a = abs(x)
    if a >= 1e7:
        return f"\u20b9{x/1e7:,.2f} Cr"
    if a >= 1e5:
        return f"\u20b9{x/1e5:,.1f} L"
    return f"\u20b9{x:,.0f}"


def kpi(col, label, value, sub="", tone="good"):
    col.markdown(
        f'<div class="kpi {tone}"><div class="lab">{label}</div>'
        f'<div class="val">{value}</div><div class="sub">{sub}</div></div>',
        unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def _load(file_bytes):
    return E.load_workbook(io.BytesIO(file_bytes))


st.title("Levisol Supply Chain Planner")
st.caption("Monthly production sourcing, hub routing and inventory norms — "
           "built for the planning team, not for data scientists.")

# ---------------- Sidebar: all editable inputs ----------------
with st.sidebar:
    st.header("1 · Data")
    up = st.file_uploader("Case data workbook (.xlsx)", type=["xlsx"])
    st.caption("Upload a revised workbook to re-plan against new demand, "
               "costs or capacities.")
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "Castrol_Case_comp_data.xlsx")

    st.header("2 · Plant capacity  (kl/month)")
    st.caption("Set a line to 0 to simulate an outage.")
    cap = copy.deepcopy(E.DEFAULT_CAP)
    for P in E.PLANTS:
        with st.expander(f"{P}", expanded=False):
            for L in E.LINES:
                cap[P][L] = st.number_input(f"{P} · {L}", min_value=0, max_value=20000,
                                            value=int(E.DEFAULT_CAP[P][L]), step=25,
                                            key=f"cap_{P}_{L}")

    st.header("3 · Production cost  (₹/kl)")
    pc = {P: float(st.number_input(P, min_value=0, max_value=100000,
                                   value=int(E.DEFAULT_PROD_COST[P]), step=250,
                                   key=f"pc_{P}")) for P in E.PLANTS}

    st.header("4 · Freight")
    fscale = st.slider("Freight cost multiplier", 0.5, 2.0, 1.0, 0.05,
                       help="Scales every plant→hub and hub→CFA rate.")
    cph = {k: v * fscale for k, v in E.DEFAULT_C_PH.items()}
    chc = {k: (a * fscale, b * fscale) for k, (a, b) in E.DEFAULT_C_HC.items()}

    st.header("5 · Service policy")
    st.caption("Target fill rate by SKU tier.")
    tf = {t: st.slider(f"Tier {t}", 0.90, 0.99, float(E.TIER_FILL[t]), 0.01,
                       key=f"tf_{t}") for t in ['A', 'B', 'C', 'D']}

    st.header("6 · Demand scenario")
    dm = {t: st.slider(f"Tier {t} demand ×", 0.5, 2.0, 1.0, 0.05, key=f"dm_{t}")
          for t in ['A', 'B', 'C', 'D']}

    st.header("7 · Cost assumptions")
    protect = st.checkbox("Protect contractual SKUs absolutely", value=True,
                          help="Price a contractual breach so highly that the plan will "
                               "always serve these SKUs in full while capacity allows. "
                               "Verified to cost effectively nothing at base capacity.")
    cm = 50.0 if protect else st.slider(
        "Contractual breach multiplier", 1.0, 10.0, 3.0, 0.5,
        help="Escalation applied to the stated penalty for contractual SKUs.")
    hpf = st.slider("Hub shortfall penalty (× SKU penalty)", 0.0, 1.0, 0.10, 0.05,
                    help="A hub buffer gap does not lose a sale today — it raises "
                         "future stockout risk. Priced as a fraction of full penalty.")
    hold = st.number_input("Hub holding cost (₹/kl-month)", 0, 5000, 180, 20,
                           help="Working capital carried on hub stock.")

    st.header("8 · Solver")
    tl = st.slider("Time limit (s)", 10, 120, 40, 5)
    gap = st.select_slider("Optimality gap", [0.02, 0.01, 0.005, 0.002], value=0.005)

# ---------------- Load ----------------
try:
    if up is not None:
        d = _load(up.getvalue())
        src = up.name
    else:
        with open(default_path, "rb") as f:
            d = _load(f.read())
        src = default_path
except FileNotFoundError:
    st.error("No data workbook found. Upload the case data file in the sidebar to begin.")
    st.stop()
except Exception as ex:
    st.error(f"Could not read that workbook: {ex}")
    st.caption("Expected the case data file with sheets D, E, G, H, I and J.")
    st.stop()

st.markdown(f'<div class="banner">Planning from <b>{src}</b> — '
            f'{len(d["skus"])} SKUs · {len(E.CFAS)} CFAs · 2 hubs · 3 plants</div>',
            unsafe_allow_html=True)

run = st.button("Run plan", type="primary", use_container_width=True)
if run:
    st.session_state.go = True
if not st.session_state.get("go"):
    st.info("Set your inputs in the sidebar, then choose **Run plan**.")
    st.stop()

# ---------------- Solve ----------------
t0 = time.time()
with st.spinner("Optimising production and routing…"):
    norms, req, ss_cfa, hub_ss = E.build_norms(d, tier_fill=tf, demand_mult=dm)
    r = E.optimise(d, req, hub_ss, cap=cap, prod_cost=pc, c_ph=cph, c_hc=chc,
                   contract_mult=cm, hub_pen_frac=hpf, hold_cost=hold,
                   time_limit=tl, mip_gap=gap, tier_fill=tf)
elapsed = time.time() - t0

if r is None:
    st.markdown('<div class="banner banner-red"><b>No plan returned.</b> '
                'The solver could not produce a solution in the time allowed. '
                'Raise the time limit or widen the optimality gap in the sidebar.</div>',
                unsafe_allow_html=True)
    st.stop()

F = E.to_frames(d, r, norms)
c = r['costs']
econ = c['total'] - c['policy_breach']   # cash cost; policy breach is a shadow price
total_fc = sum(d['jan'].get(k, 0) * dm.get(d['tier'][k[0]], 1.0) for k in d['jan'])

# ---------------- Headline ----------------
k1, k2, k3, k4, k5 = st.columns(5)
kpi(k1, "Total OpEx", inr(econ),
    f"₹{econ/max(r['produced_kl'],1):,.0f} per kl (cash)")
kpi(k2, "Fill rate", f"{100*r['fill_rate']:.2f}%",
    f"{r['unmet_kl']:.1f} kl unmet",
    "good" if r['fill_rate'] > 0.98 else "warn")
kpi(k3, "Production", f"{r['produced_kl']:,.0f} kl", "across 3 plants")
kpi(k4, "Hub SS gap", f"{r['hub_short_kl']:,.0f} kl",
    "below hub buffer target", "warn" if r['hub_short_kl'] > 1 else "good")
kpi(k5, "Solve time", f"{elapsed:.1f}s", f"gap ≤ {gap:.1%}")

if c['policy_breach'] > 0:
    tv = ", ".join(f"Tier {t}: {v:,.0f} kl" for t, v in r['tier_viol'].items())
    st.markdown(
        f'<div class="banner banner-red"><b>Service policy breached.</b> '
        f'Capacity is too tight to hold the tier fill-rate targets. '
        f'Shortfall beyond policy — {tv}. Scarcity has been absorbed by the '
        f'lowest tiers first, and contractual SKUs protected.</div>',
        unsafe_allow_html=True)

if r['unmet_kl'] > 0.01:
    n_con = int((F['unmet']['Contractual'] == 'YES').sum()) if not F['unmet'].empty else 0
    tone = "banner-red" if n_con else "banner"
    st.markdown(
        f'<div class="banner {tone}"><b>Demand not fully met.</b> '
        f'{r["unmet_kl"]:.2f} kl short across {len(F["unmet"])} SKU–CFA lines, '
        f'costing {inr(c["penalty_unmet"])} in penalties. '
        f'{n_con} contractual line(s) affected. See <b>Unmet demand</b> below.</div>',
        unsafe_allow_html=True)
else:
    st.markdown('<div class="banner"><b>All demand met.</b> '
                'Every CFA requirement is satisfied within capacity.</div>',
                unsafe_allow_html=True)

# ---------------- Tabs ----------------
T = st.tabs(["Cost summary", "Production plan", "Routing", "Network map",
             "Unmet demand", "Inventory norms", "Capacity & shadow price",
             "Service–cost frontier", "Sensitivity", "Scenarios",
             "Compare to baseline"])

with T[0]:
    cs = pd.DataFrame([
        {"Component": "Production", "Cost (₹)": c['production']},
        {"Component": "Freight — plant to hub", "Cost (₹)": c['freight_plant_hub']},
        {"Component": "Freight — hub to CFA", "Cost (₹)": c['freight_hub_cfa']},
        {"Component": "Penalty — unmet demand", "Cost (₹)": c['penalty_unmet']},
        {"Component": "Penalty — hub safety-stock gap", "Cost (₹)": c['penalty_hub_ss']},
        {"Component": "Holding — hub stock", "Cost (₹)": c['holding']},
        {"Component": "TOTAL CASH COST", "Cost (₹)": econ},
    ])
    cs["Amount"] = cs["Cost (₹)"].map(inr)
    cs["Share"] = (cs["Cost (₹)"] / econ).map(lambda v: f"{v:.1%}")
    st.dataframe(cs[["Component", "Amount", "Share"]],
                 use_container_width=True, hide_index=True)
    st.bar_chart(cs.iloc[:-1].set_index("Component")["Cost (₹)"])
    if c['policy_breach'] > 0:
        st.warning(f"Policy-breach shadow cost: {inr(c['policy_breach'])}. "
                   "This is not cash — it is the prioritisation weight that forces "
                   "scarcity onto low-tier SKUs first. Excluded from the cash total.")
    st.subheader("Service by tier")
    tv = []
    for t in ['A', 'B', 'C', 'D']:
        dt = sum(v for (s_, C), v in req.items() if d['tier'][s_] == t)
        ut = sum(v for (s_, C), v in r['short'].items() if d['tier'][s_] == t)
        tv.append({"Tier": t, "Target": f"{tf[t]:.0%}",
                   "Achieved": f"{(1-ut/dt if dt else 1):.2%}",
                   "Requirement (kl)": round(dt, 1), "Unmet (kl)": round(ut, 1)})
    st.dataframe(pd.DataFrame(tv), use_container_width=True, hide_index=True)
    load = (F['production'].groupby('Plant')['Volume (kl)'].sum()
            .reindex(E.PLANTS).fillna(0))
    st.subheader("Plant loading")
    st.dataframe(pd.DataFrame({"Plant": load.index, "Volume (kl)": load.values,
                               "Cost ₹/kl": [pc[p] for p in load.index]}),
                 use_container_width=True, hide_index=True)

with T[1]:
    st.caption("Volumes respect the 25 kl batch rule at every plant.")
    st.dataframe(F['production'], use_container_width=True, hide_index=True, height=440)

with T[2]:
    a, b = st.columns(2)
    a.subheader("Plant → hub")
    a.dataframe(F['plant_hub'].groupby(['Plant', 'Hub'], as_index=False)
                .agg({'Volume (kl)': 'sum', 'Freight (Rs)': 'sum'}).round(1),
                use_container_width=True, hide_index=True)
    b.subheader("Hub → CFA")
    b.dataframe(F['hub_cfa'].groupby(['Hub', 'CFA'], as_index=False)
                .agg({'Volume (kl)': 'sum', 'Freight (Rs)': 'sum'}).round(1),
                use_container_width=True, hide_index=True)
    st.subheader("Full routing by SKU")
    st.dataframe(F['hub_cfa'], use_container_width=True, hide_index=True, height=300)

with T[3]:
    st.caption("Where the volume actually moves. Arc height is proportional to "
               "volume; plants feed hubs, hubs feed CFAs.")
    try:
        import pydeck as pdk
        ph_agg = (F['plant_hub'].groupby(['Plant', 'Hub'], as_index=False)
                  ['Volume (kl)'].sum())
        hc_agg = (F['hub_cfa'].groupby(['Hub', 'CFA'], as_index=False)
                  ['Volume (kl)'].sum())
        arcs = []
        for _, x in ph_agg.iterrows():
            a, b = E.COORDS[x.Plant], E.COORDS[x.Hub]
            arcs.append({"from_lon": a[1], "from_lat": a[0], "to_lon": b[1],
                         "to_lat": b[0], "vol": float(x['Volume (kl)']),
                         "label": f"{x.Plant} → {x.Hub}: {x['Volume (kl)']:,.0f} kl",
                         "col": [0, 132, 61]})
        for _, x in hc_agg.iterrows():
            a, b = E.COORDS[x.Hub], E.COORDS[x.CFA]
            arcs.append({"from_lon": a[1], "from_lat": a[0], "to_lon": b[1],
                         "to_lat": b[0], "vol": float(x['Volume (kl)']),
                         "label": f"{x.Hub} → {x.CFA}: {x['Volume (kl)']:,.0f} kl",
                         "col": [201, 42, 42]})
        adf = pd.DataFrame(arcs)
        mx = max(adf["vol"].max(), 1)
        adf["w"] = 1 + 11 * adf["vol"] / mx
        nodes = []
        for P in E.PLANTS:
            v = F['production'][F['production'].Plant == P]['Volume (kl)'].sum()
            nodes.append({"name": f"{P} plant", "lat": E.COORDS[P][0],
                          "lon": E.COORDS[P][1], "vol": float(v),
                          "col": [11, 42, 59], "r": 55000})
        for H in E.HUBS:
            v = ph_agg[ph_agg.Hub == H]['Volume (kl)'].sum()
            nodes.append({"name": f"{H} hub", "lat": E.COORDS[H][0],
                          "lon": E.COORDS[H][1], "vol": float(v),
                          "col": [0, 132, 61], "r": 45000})
        for C in E.CFAS:
            v = hc_agg[hc_agg.CFA == C]['Volume (kl)'].sum()
            nodes.append({"name": C, "lat": E.COORDS[C][0], "lon": E.COORDS[C][1],
                          "vol": float(v), "col": [201, 42, 42], "r": 32000})
        ndf = pd.DataFrame(nodes)
        st.pydeck_chart(pdk.Deck(
            map_style=None,
            initial_view_state=pdk.ViewState(latitude=22.5, longitude=80.0,
                                             zoom=3.7, pitch=42),
            layers=[
                pdk.Layer("ArcLayer", data=adf, get_source_position=["from_lon", "from_lat"],
                          get_target_position=["to_lon", "to_lat"],
                          get_source_color="col", get_target_color="col",
                          get_width="w", pickable=True, auto_highlight=True),
                pdk.Layer("ScatterplotLayer", data=ndf, get_position=["lon", "lat"],
                          get_fill_color="col", get_radius="r", pickable=True,
                          opacity=0.75),
            ],
            tooltip={"text": "{label}{name}\n{vol} kl"}))
        a, b = st.columns(2)
        a.dataframe(ph_agg.round(0), use_container_width=True, hide_index=True)
        b.dataframe(hc_agg.round(0).sort_values('Volume (kl)', ascending=False),
                    use_container_width=True, hide_index=True)
    except Exception as ex:
        st.warning(f"Map unavailable ({ex}). The routing tables above carry the "
                   "same information.")

with T[4]:
    if F['unmet'].empty:
        st.success("Nothing is short. Every CFA requirement is met in full.")
    else:
        st.caption("What we chose not to supply, and what it costs. "
                   "Contractual lines are protected first.")
        st.dataframe(F['unmet'], use_container_width=True, hide_index=True)
        st.metric("Total penalty", inr(c['penalty_unmet']))

with T[5]:
    st.caption("Safety stock buffers forecast error and lead-time variability: "
               "SS = z·√(L·σ_d² + d̄²·σ_L²).  Reorder point = d̄·L + SS.")
    st.dataframe(F['norms'], use_container_width=True, hide_index=True, height=440)
    tot_ss = F['norms']['Safety stock (kl)'].sum()
    hub_tot = sum(hub_ss.values())
    a, b, cc2 = st.columns(3)
    a.metric("CFA safety stock", f"{tot_ss:,.0f} kl")
    b.metric("Hub safety stock (pooled)", f"{hub_tot:,.0f} kl")
    cc2.metric("Pooling saving", f"{100*(1-hub_tot/max(tot_ss,1)):.0f}%",
               help="Hub buffer vs the sum of the CFA buffers it covers.")

with T[6]:
    cap_df = F['capacity'].copy()
    with st.spinner("Valuing capacity…"):
        sp = E.shadow_prices(d, req, hub_ss, cap=cap, prod_cost=pc, c_ph=cph, c_hc=chc,
                             contract_mult=cm, hub_pen_frac=hpf, hold_cost=hold,
                             tier_fill=tf)
    cap_df["Shadow price (₹/kl)"] = [round(sp.get((p, l), 0.0))
                                     for p, l in zip(cap_df.Plant, cap_df.Line)]
    st.caption("Shadow price = rupees saved per additional kl/month of that line. "
               "Zero means the line has spare capacity worth nothing at the margin. "
               "Valid for marginal changes only.")
    st.dataframe(cap_df, use_container_width=True, hide_index=True)
    top = cap_df.sort_values("Shadow price (₹/kl)", ascending=False).iloc[0]
    if top["Shadow price (₹/kl)"] > 0:
        st.markdown(
            f'<div class="banner"><b>Debottleneck first:</b> {top.Plant} · {top.Line}. '
            f'Each extra kl/month saves ₹{top["Shadow price (₹/kl)"]:,.0f} — '
            f'about {inr(top["Shadow price (₹/kl)"]*100*12)} a year per '
            f'100 kl/month added.</div>', unsafe_allow_html=True)

with T[7]:
    st.caption("What each point of service costs. Re-solves the plan at a uniform "
               "target fill rate across all tiers.")
    if st.button("Build frontier (6 solves)"):
        rows = []
        bar = st.progress(0.0)
        for i, (fill, _z) in enumerate(sorted(E.Z_TABLE.items())):
            nrm2, req2, ss2, hub2 = E.build_norms(
                d, tier_fill={t: fill for t in 'ABCD'}, demand_mult=dm)
            rr = E.optimise(d, req2, hub2, cap=cap, prod_cost=pc, c_ph=cph, c_hc=chc,
                            contract_mult=cm, hub_pen_frac=hpf, hold_cost=hold,
                            time_limit=20, mip_gap=0.01,
                            tier_fill={t: fill for t in 'ABCD'})
            if rr:
                rows.append({"Service level": fill,
                             "Total cost (₹)": rr['costs']['total'] - rr['costs']['policy_breach'],
                             "Production (kl)": rr['produced_kl'],
                             "Safety stock (kl)": sum(ss2.values()) + sum(hub2.values()),
                             "Unmet (kl)": rr['unmet_kl']})
            bar.progress((i + 1) / len(E.Z_TABLE))
        fdf = pd.DataFrame(rows)
        fdf["Marginal ₹ per service point"] = [np.nan] + [
            (fdf["Total cost (₹)"][i] - fdf["Total cost (₹)"][i - 1]) /
            ((fdf["Service level"][i] - fdf["Service level"][i - 1]) * 100)
            for i in range(1, len(fdf))]
        st.session_state.frontier = fdf
    if "frontier" in st.session_state:
        fdf = st.session_state.frontier
        fshow = fdf.copy()
        fshow["Total cost"] = fshow["Total cost (₹)"].map(inr)
        fshow["Marginal cost / service pt"] = fshow[
            "Marginal ₹ per service point"].map(
            lambda v: inr(v) if pd.notna(v) else "—")
        st.dataframe(fshow[["Service level", "Total cost", "Production (kl)",
                            "Safety stock (kl)", "Unmet (kl)",
                            "Marginal cost / service pt"]].style.format(
            {"Service level": "{:.0%}", "Production (kl)": "{:,.0f}",
             "Safety stock (kl)": "{:,.0f}", "Unmet (kl)": "{:.1f}"}),
            use_container_width=True, hide_index=True)
        _unused = fdf.style.format({"Service level": "{:.0%}",
                                       "Total cost (₹)": "{:,.0f}",
                                       "Production (kl)": "{:,.0f}",
                                       "Safety stock (kl)": "{:,.0f}",
                                       "Unmet (kl)": "{:.1f}",
                                       "Marginal ₹ per service point": "{:,.0f}"})
        st.line_chart(fdf.set_index("Service level")["Total cost (₹)"])
        st.caption("The curve steepens above ~97%: each further point of service "
                   "costs disproportionately more. This is why the tiered policy "
                   "(A 98% / B 97% / C–D 92%) beats a uniform target — it buys "
                   "protection only where volume justifies it.")

with T[8]:
    st.caption("Which inputs actually move the cost. Each bar re-solves the plan with "
               "that one input shifted, holding everything else fixed. "
               "Computed on the LP relaxation, so it runs in about a second.")
    swing = st.slider("Shift each input by ±", 0.05, 0.50, 0.20, 0.05,
                      format="%.0f%%", key="sens_swing")
    if st.button("Run sensitivity"):
        with st.spinner("Testing each input…"):
            base_lp = E.lp_cost(d, req, hub_ss, cap=cap, prod_cost=pc, c_ph=cph,
                                c_hc=chc, contract_mult=cm, hub_pen_frac=hpf,
                                hold_cost=hold, tier_fill=tf)
            out = []
            for name, lo_kw, hi_kw in [
                ("Production cost (all plants)",
                 dict(prod_cost={k: v*(1-swing) for k, v in pc.items()}),
                 dict(prod_cost={k: v*(1+swing) for k, v in pc.items()})),
                ("Freight rates",
                 dict(c_ph={k: v*(1-swing) for k, v in cph.items()},
                      c_hc={k: (a*(1-swing), b*(1-swing)) for k, (a, b) in chc.items()}),
                 dict(c_ph={k: v*(1+swing) for k, v in cph.items()},
                      c_hc={k: (a*(1+swing), b*(1+swing)) for k, (a, b) in chc.items()})),
                ("Kolkata production cost",
                 dict(prod_cost={**pc, 'KOL': pc['KOL']*(1-swing)}),
                 dict(prod_cost={**pc, 'KOL': pc['KOL']*(1+swing)})),
                ("Total plant capacity",
                 dict(cap={P: {L: int(v*(1-swing)) for L, v in cap[P].items()}
                           for P in E.PLANTS}),
                 dict(cap={P: {L: int(v*(1+swing)) for L, v in cap[P].items()}
                           for P in E.PLANTS})),
                ("Hub shortfall penalty",
                 dict(hub_pen_frac=hpf*(1-swing)),
                 dict(hub_pen_frac=hpf*(1+swing))),
            ]:
                kw = dict(cap=cap, prod_cost=pc, c_ph=cph, c_hc=chc,
                          contract_mult=cm, hub_pen_frac=hpf, hold_cost=hold,
                          tier_fill=tf)
                lo_c = E.lp_cost(d, req, hub_ss, **{**kw, **lo_kw})
                hi_c = E.lp_cost(d, req, hub_ss, **{**kw, **hi_kw})
                out.append({"Input": name,
                            f"−{swing:.0%}": lo_c, f"+{swing:.0%}": hi_c,
                            "Swing (₹)": abs(hi_c - lo_c),
                            "Swing %": 100*abs(hi_c - lo_c)/base_lp})
            sdf = pd.DataFrame(out).sort_values("Swing (₹)", ascending=False)
            st.session_state.sens = (sdf, base_lp)
    if "sens" in st.session_state:
        sdf, base_lp = st.session_state.sens
        show = sdf.copy()
        for cl in show.columns[1:4]:
            show[cl] = show[cl].map(inr)
        show["Swing %"] = sdf["Swing %"].map(lambda v: f"{v:.1f}%")
        st.dataframe(show, use_container_width=True, hide_index=True)
        st.bar_chart(sdf.set_index("Input")["Swing (₹)"])
        top = sdf.iloc[0]
        st.markdown(
            f'<div class="banner"><b>{top.Input}</b> is the input the plan is most '
            f'exposed to — a ±{swing:.0%} move swings cost by {inr(top["Swing (₹)"])} '
            f'({top["Swing %"]:.1f}%). Everything below it matters less.</div>',
            unsafe_allow_html=True)

with T[9]:
    st.caption("Run any scenario, add it here, and compare them side by side.")
    if "scen" not in st.session_state:
        st.session_state.scen = []
    nm = st.text_input("Name this scenario",
                       value=f"Scenario {len(st.session_state.scen)+1}")
    a, b = st.columns(2)
    if a.button("Add current plan to comparison"):
        tiers = {}
        for t in ['A', 'B', 'C', 'D']:
            dt = sum(v for (s_, C), v in req.items() if d['tier'][s_] == t)
            ut = sum(v for (s_, C), v in r['short'].items() if d['tier'][s_] == t)
            tiers[t] = 100*(1 - ut/dt) if dt else 100.0
        st.session_state.scen.append({
            "Scenario": nm, "Total OpEx": econ, "Produced (kl)": r['produced_kl'],
            "Fill %": 100*r['fill_rate'], "Unmet (kl)": r['unmet_kl'],
            "Hub SS gap (kl)": r['hub_short_kl'],
            "Tier A %": tiers['A'], "Tier B %": tiers['B'],
            "Tier C %": tiers['C'], "Tier D %": tiers['D'],
            "_raw": econ})
        st.success(f"Added “{nm}”.")
    if b.button("Clear all scenarios"):
        st.session_state.scen = []
    if st.session_state.scen:
        sc = pd.DataFrame(st.session_state.scen)
        base0 = sc["_raw"].iloc[0]
        sc["vs first"] = sc["_raw"].map(
            lambda v: "—" if v == base0 else f"{100*(v/base0-1):+.0f}%")
        disp = sc.drop(columns=["_raw"]).copy()
        disp["Total OpEx"] = sc["Total OpEx"].map(inr)
        for cl in ["Fill %", "Tier A %", "Tier B %", "Tier C %", "Tier D %"]:
            disp[cl] = sc[cl].map(lambda v: f"{v:.2f}%")
        for cl in ["Produced (kl)", "Unmet (kl)", "Hub SS gap (kl)"]:
            disp[cl] = sc[cl].map(lambda v: f"{v:,.1f}")
        st.dataframe(disp, use_container_width=True, hide_index=True)
        st.bar_chart(sc.set_index("Scenario")["_raw"].rename("Total OpEx (₹)"))
        st.caption("Change an input in the sidebar, press Run plan, then add it here. "
                   "Suggested set: base · Kolkata outage · demand spike · "
                   "capacity −50% · freight +50%.")
    else:
        st.info("No scenarios added yet. Run a plan, then choose "
                "**Add current plan to comparison**.")

with T[10]:
    st.caption("Save the current plan as the baseline, change any input, "
               "re-run, and see exactly what moved.")
    a, b = st.columns(2)
    if a.button("Save as baseline"):
        st.session_state.base = dict(costs=dict(c), fill=r['fill_rate'],
                                     prod=r['produced_kl'], unmet=r['unmet_kl'],
                                     plan=F['production'].copy())
        st.success("Baseline saved.")
    if b.button("Clear baseline"):
        st.session_state.pop("base", None)
    if "base" in st.session_state:
        bs = st.session_state.base
        rows = [("Total OpEx (Rs)", bs['costs']['total'] - bs['costs']['policy_breach'], econ),
                ("Production (kl)", bs['prod'], r['produced_kl']),
                ("Fill rate", bs['fill'], r['fill_rate']),
                ("Unmet (kl)", bs['unmet'], r['unmet_kl'])]
        def fmt(m, v):
            return inr(v) if "Rs" in m else (f"{v:.2%}" if "Fill" in m
                                             else f"{v:,.1f}")
        dd = pd.DataFrame([{"Metric": m, "Baseline": fmt(m, x),
                            "Current": fmt(m, y), "Δ": fmt(m, y - x),
                            "Δ %": (f"{(y/x-1)*100:+.1f}%" if x else "—")}
                           for m, x, y in rows])
        st.dataframe(dd, use_container_width=True, hide_index=True)
        m = bs['plan'].merge(F['production'], on=['SKU', 'Plant'], how='outer',
                             suffixes=(' base', ' now')).fillna(0)
        m['Δ kl'] = m['Volume (kl) now'] - m['Volume (kl) base']
        moved = m[m['Δ kl'].abs() > 0.01][['SKU', 'Plant', 'Volume (kl) base',
                                           'Volume (kl) now', 'Δ kl']]
        st.subheader(f"Production moves ({len(moved)} SKU–plant lines changed)")
        st.dataframe(moved.sort_values('Δ kl', key=abs, ascending=False),
                     use_container_width=True, hide_index=True, height=280)
    else:
        st.info("No baseline saved yet.")

# ---------------- Export ----------------
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine='openpyxl') as w:
    pd.DataFrame([{"Component": k, "Cost (Rs)": v} for k, v in c.items()]).to_excel(
        w, sheet_name="Cost summary", index=False)
    F['production'].to_excel(w, sheet_name="Production plan", index=False)
    F['plant_hub'].to_excel(w, sheet_name="Routing plant-hub", index=False)
    F['hub_cfa'].to_excel(w, sheet_name="Routing hub-CFA", index=False)
    F['unmet'].to_excel(w, sheet_name="Unmet demand", index=False)
    F['capacity'].to_excel(w, sheet_name="Capacity", index=False)
    F['norms'].to_excel(w, sheet_name="Inventory norms", index=False)
st.download_button("Download full plan (Excel)", buf.getvalue(),
                   file_name="Levisol_plan.xlsx", use_container_width=True,
                   mime="application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")
