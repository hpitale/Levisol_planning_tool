"""
LEVISOL PLANNING ENGINE
MILP production-sourcing + 2-echelon distribution optimiser.
Shortage and hub-shortfall are PRICED DECISION VARIABLES, so the model is
structurally always feasible: it can never crash, it can only report a
plan whose unmet demand carries a stated cost.
Solver: HiGHS (scipy.optimize.milp)
"""
from __future__ import annotations
import re
import numpy as np
import pandas as pd
from scipy.optimize import milp, linprog, LinearConstraint, Bounds
from scipy import sparse

MONTHS = ['Jul-25 (in kL)', 'Aug-25 (in kL)', 'Sep-25 (in kL)',
          'Oct-25 (in kL)', 'Nov-25 (in kL)', 'Dec-25 (in kL)']
BATCH = 25.0
PLANTS = ['BOM', 'AHM', 'KOL']
HUBS = ['MHW', 'MHE']
LINES = ['<=1.5LT', '3-5LT', '7-20LT', '50LT', '180-210LT']
CFAS = ['Guwahati CFA', 'Kolkata CFA', 'Jamshedpur CFA', 'Kanpur CFA', 'Haryana CFA',
        'Rajpura CFA', 'Bhiwandi CFA', 'Bangalore CFA', 'Ahmedabad CFA', 'Hyderabad CFA']

DEFAULT_PROD_COST = {'BOM': 12000.0, 'AHM': 12500.0, 'KOL': 9000.0}
DEFAULT_CAP = {
    'BOM': {'<=1.5LT': 1200, '3-5LT': 900, '7-20LT': 1000, '50LT': 0, '180-210LT': 2450},
    'AHM': {'<=1.5LT': 1600, '3-5LT': 550, '7-20LT': 1000, '50LT': 220, '180-210LT': 2200},
    'KOL': {'<=1.5LT': 1200, '3-5LT': 500, '7-20LT': 650, '50LT': 0, '180-210LT': 200},
}
DEFAULT_C_PH = {('BOM', 'MHW'): 1000., ('BOM', 'MHE'): 8000.,
                ('AHM', 'MHW'): 4000., ('AHM', 'MHE'): 5000.,
                ('KOL', 'MHW'): 10000., ('KOL', 'MHE'): 1100.}
DEFAULT_C_HC = {
    'Guwahati CFA': (7800., 4200.), 'Kolkata CFA': (8000., 1100.),
    'Jamshedpur CFA': (5100., 1700.), 'Kanpur CFA': (3800., 3100.),
    'Haryana CFA': (4400., 4500.), 'Rajpura CFA': (4900., 5100.),
    'Bhiwandi CFA': (1000., 10000.), 'Bangalore CFA': (3800., 5100.),
    'Ahmedabad CFA': (1900., 4900.), 'Hyderabad CFA': (2600., 4000.),
}
Z_TABLE = {0.90: 1.282, 0.92: 1.405, 0.95: 1.645, 0.97: 1.881, 0.98: 2.054, 0.99: 2.326}
TIER_FILL = {'A': 0.98, 'B': 0.97, 'C': 0.92, 'D': 0.92}
TIERS = ['A', 'B', 'C', 'D']

# Cost of breaching a tier's fill-rate POLICY, Rs/kl.
# Set above the highest SKU penalty (Rs 230k/kl) so service policy binds before pure
# economics, and escalated by tier so scarcity is absorbed by low-tier products first.
# This is what encodes Exhibit F's instruction that higher tiers are prioritised in a
# shortfall -- the stated penalty costs alone do NOT correlate with tier.
TIER_VIOL_COST = {'A': 1_200_000., 'B': 900_000., 'C': 600_000., 'D': 300_000.}


def _unit_litres(ps: str) -> float:
    m = re.search(r'X\s*([\d.]+)\s*(ML|LT|KG)', str(ps))
    if not m:
        m = re.search(r'([\d.]+)\s*(ML|LT|KG)', str(ps))
    v, u = float(m.group(1)), m.group(2)
    return v / 1000 if u == 'ML' else v


def line_of(ps: str) -> str:
    u = _unit_litres(ps)
    if u <= 1.5:
        return '<=1.5LT'
    if u <= 5:
        return '3-5LT'
    if u <= 20:
        return '7-20LT'
    if u == 50:
        return '50LT'
    return '180-210LT'


def load_workbook(path) -> dict:
    """Read the case data file into the engine's internal structures."""
    D = pd.read_excel(path, sheet_name='D -SKU Portfolio+Penalty matrix', skiprows=2)
    E = pd.read_excel(path, sheet_name='E - Source + LT data', skiprows=2)
    G = pd.read_excel(path, sheet_name='G - Sales History', skiprows=2)
    H = pd.read_excel(path, sheet_name='H - Forecast History', skiprows=3)
    I = pd.read_excel(path, sheet_name='I - Expected opening Inventory', skiprows=3)
    J = pd.read_excel(path, sheet_name='J - Jan Forecast', skiprows=3)

    d = {'skus': D['Product Name'].tolist(),
         'packsize': dict(zip(D['Product Name'], D['Pack size'])),
         'line': {s: line_of(p) for s, p in zip(D['Product Name'], D['Pack size'])},
         'penalty': dict(zip(D['Product Name'], D['Penalty cost (per kL)'].astype(float))),
         'contractual': {s: str(c).strip().lower() != 'no'
                         for s, c in zip(D['Product Name'], D['Contractual?'])}}

    G['avg'] = G[MONTHS].mean(axis=1)
    vol = G.groupby('Product Name')['avg'].sum().sort_values(ascending=False)
    cum = vol.cumsum() / vol.sum()
    d['tier'] = {s: ('A' if c <= .5 else 'B' if c <= .8 else 'C' if c <= .95 else 'D')
                 for s, c in cum.items()}
    d['avg_monthly'] = vol.to_dict()

    Gm = G[['Product Name', 'CFA'] + MONTHS]
    Hm = H[['Product Name', 'CFA'] + MONTHS].copy()
    Hm.columns = ['Product Name', 'CFA'] + ['F_' + m for m in MONTHS]
    m = Gm.merge(Hm, on=['Product Name', 'CFA'], how='left')
    err = m[MONTHS].values - m[['F_' + x for x in MONTHS]].values
    m['d_daily'] = m[MONTHS].mean(axis=1) / 30
    m['sig_mo'] = np.nanstd(err, axis=1, ddof=1)
    ren = {'LT (Plant to Hub)(in  days)': 'LT_ph', 'LT (Hub to CFA ) (in  days)': 'LT_hc',
           'Production lead time (in  days)': 'LT_pr',
           'Production variability (in  days)': 'v_pr',
           'Transit lead variability (in  days)': 'v_tr'}
    m = m.merge(E[['Product Name', 'CFA', 'Source'] + list(ren)].rename(columns=ren),
                on=['Product Name', 'CFA'], how='left')
    m['hub'] = m['Source'].map({'East': 'MHE', 'Rest of India': 'MHW'})
    m['L'] = m['LT_pr'] + m['LT_ph'] + m['LT_hc']
    m['sigL'] = np.sqrt(m['v_pr'] ** 2 + m['v_tr'] ** 2)
    m['sig_d'] = m['sig_mo'] / np.sqrt(30)
    m['Tier'] = m['Product Name'].map(d['tier'])
    d['norm'] = m

    d['jan'] = {(r['Product Name'], r['CFA']): float(r['Jan -2026 (in kL)'])
                for _, r in J.iterrows()}
    Ic = I[I['CFA'].isin(CFAS)]
    d['open_cfa'] = {(r['Product Name'], r['CFA']): float(r['Jan -2026 (in kL)'])
                     for _, r in Ic.iterrows()}
    hmap = {'Mother Hub West': 'MHW', 'Mother Hub East': 'MHE'}
    Ih = I[I['CFA'].isin(hmap)]
    d['open_hub'] = {(r['Product Name'], hmap[r['CFA']]): float(r['Jan -2026 (in kL)'])
                     for _, r in Ih.iterrows()}
    return d


def build_norms(d, tier_fill=None, demand_mult=None):
    """Safety stock, ROP, days-of-cover per SKU x CFA, plus pooled hub SS."""
    tier_fill = tier_fill or TIER_FILL
    demand_mult = demand_mult or {}
    m = d['norm'].copy()
    m['fill'] = m['Tier'].map(tier_fill)
    m['z'] = m['fill'].map(lambda f: float(np.interp(f, list(Z_TABLE), list(Z_TABLE.values()))))
    mult = m['Tier'].map(lambda t: demand_mult.get(t, 1.0)).astype(float)
    m['d_daily_adj'] = m['d_daily'] * mult
    m['sig_d_adj'] = m['sig_d'] * mult
    m['SS'] = m['z'] * np.sqrt(m['L'] * m['sig_d_adj'] ** 2 + m['d_daily_adj'] ** 2 * m['sigL'] ** 2)
    m['ROP'] = m['d_daily_adj'] * m['L'] + m['SS']
    m['DoC'] = np.where(m['d_daily_adj'] > 0, m['ROP'] / m['d_daily_adj'], 0.0)

    req, ss_cfa = {}, {}
    for _, r in m.iterrows():
        k = (r['Product Name'], r['CFA'])
        ss_cfa[k] = r['SS']
        fc = d['jan'].get(k, 0.0) * demand_mult.get(r['Tier'], 1.0)
        req[k] = max(0.0, fc + r['SS'] - d['open_cfa'].get(k, 0.0))

    hub_ss = {}
    for (sku, hub), g in m.groupby(['Product Name', 'hub']):
        dagg = g['d_daily_adj'].sum()
        if dagg <= 0:
            continue
        sig = np.sqrt((g['sig_d_adj'] ** 2).sum())          # risk pooling
        Lh = (g['LT_pr'] + g['LT_ph']).mean()
        sLh = g['v_pr'].mean()
        hub_ss[(sku, hub)] = 2.054 * np.sqrt(Lh * sig ** 2 + dagg ** 2 * sLh ** 2)
    return m, req, ss_cfa, hub_ss


def _assemble(d, req, hub_ss, cap, prod_cost, c_ph, c_hc,
              contract_mult, hub_pen_frac, hold_cost, integral=True,
              tier_fill=None, tier_viol_cost=None):
    tier_fill = tier_fill or TIER_FILL
    tier_viol_cost = tier_viol_cost or TIER_VIOL_COST
    skus = d['skus']
    nS, nP, nH, nC = len(skus), 3, 2, 10
    B = 43
    iTV = lambda t: nS * B + TIERS.index(t)     # tier fill-rate violation (kl)
    iN = lambda i, p: i * B + p
    iPH = lambda i, p, h: i * B + 3 + p * 2 + h
    iHC = lambda i, h, c: i * B + 9 + h * 10 + c
    iSH = lambda i, c: i * B + 29 + c
    iHS = lambda i, h: i * B + 39 + h
    iHE = lambda i, h: i * B + 41 + h
    NV = nS * B + 4

    cost = np.zeros(NV)
    integrality = np.zeros(NV)
    lb = np.zeros(NV)
    ub = np.full(NV, np.inf)

    for i, s in enumerate(skus):
        pen = d['penalty'][s] * (contract_mult if d['contractual'][s] else 1.0)
        for p, P in enumerate(PLANTS):
            cost[iN(i, p)] = prod_cost[P] * BATCH
            if integral:
                integrality[iN(i, p)] = 1
            for h, Hh in enumerate(HUBS):
                cost[iPH(i, p, h)] = c_ph[(P, Hh)]
        for h in range(nH):
            for c, C in enumerate(CFAS):
                cost[iHC(i, h, c)] = c_hc[C][h]
            cost[iHS(i, h)] = d['penalty'][s] * hub_pen_frac
            cost[iHE(i, h)] = hold_cost
        for c in range(nC):
            cost[iSH(i, c)] = pen
        need = (sum(req.get((s, C), 0.) for C in CFAS)
                + sum(hub_ss.get((s, Hh), 0.) for Hh in HUBS))
        nmax = int(np.ceil(need / BATCH)) + 1
        for p in range(nP):
            ub[iN(i, p)] = nmax
    for t in TIERS:
        cost[iTV(t)] = tier_viol_cost[t]

    rows, cols, vals, lo, hi = [], [], [], [], []
    r = 0

    def add(e, l, h_):
        nonlocal r
        for cc, vv in e:
            rows.append(r); cols.append(cc); vals.append(vv)
        lo.append(l); hi.append(h_); r += 1

    for i, s in enumerate(skus):
        for p in range(nP):
            add([(iN(i, p), BATCH)] + [(iPH(i, p, h), -1.) for h in range(nH)], 0, 0)
        for h, Hh in enumerate(HUBS):
            e = [(iPH(i, p, h), 1.) for p in range(nP)]
            e += [(iHC(i, h, c), -1.) for c in range(nC)]
            e += [(iHE(i, h), -1.)]
            ob = d['open_hub'].get((s, Hh), 0.)
            add(e, -ob, -ob)
        for h, Hh in enumerate(HUBS):
            t = hub_ss.get((s, Hh), 0.)
            add([(iHE(i, h), 1.), (iHS(i, h), 1.)], t, np.inf)
        for c, C in enumerate(CFAS):
            R = req.get((s, C), 0.)
            add([(iHC(i, h, c), 1.) for h in range(nH)] + [(iSH(i, c), 1.)], R, R)

    # tier fill-rate policy: shortage within a tier may not exceed its allowance
    for t in TIERS:
        members = [i for i, sk in enumerate(skus) if d['tier'][sk] == t]
        req_t = sum(req.get((skus[i], C), 0.) for i in members for C in CFAS)
        if req_t <= 0:
            continue
        allow = (1.0 - tier_fill[t]) * req_t
        e = [(iSH(i, c), 1.) for i in members for c in range(nC)] + [(iTV(t), -1.)]
        add(e, -np.inf, allow)

    cap_rows = {}
    for p, P in enumerate(PLANTS):
        for L in LINES:
            e = [(iN(i, p), BATCH) for i, sk in enumerate(skus) if d['line'][sk] == L]
            if not e:
                continue
            cap_rows[(P, L)] = r
            add(e, -np.inf, float(cap[P][L]))

    A = sparse.coo_matrix((vals, (rows, cols)), shape=(r, NV)).tocsr()
    idx = dict(iN=iN, iPH=iPH, iHC=iHC, iSH=iSH, iHS=iHS, iHE=iHE, iTV=iTV)
    return A, np.array(lo), np.array(hi), cost, integrality, lb, ub, cap_rows, idx


def optimise(d, req, hub_ss, cap=None, prod_cost=None, c_ph=None, c_hc=None,
             contract_mult=3.0, hub_pen_frac=0.10, hold_cost=180.0,
             time_limit=40, mip_gap=0.005, tier_fill=None, tier_viol_cost=None):
    cap = cap or DEFAULT_CAP
    prod_cost = prod_cost or DEFAULT_PROD_COST
    c_ph = c_ph or DEFAULT_C_PH
    c_hc = c_hc or DEFAULT_C_HC
    A, lo, hi, cost, integ, lb, ub, cap_rows, ix = _assemble(
        d, req, hub_ss, cap, prod_cost, c_ph, c_hc, contract_mult, hub_pen_frac,
        hold_cost, True, tier_fill, tier_viol_cost)
    res = milp(c=cost, constraints=LinearConstraint(A, lo, hi), integrality=integ,
               bounds=Bounds(lb, ub),
               options=dict(time_limit=float(time_limit), mip_rel_gap=float(mip_gap),
                            presolve=True))
    if res.x is None:
        return None
    x = res.x
    skus = d['skus']
    prod, ph, hc, short, hubshort, hubend = {}, {}, {}, {}, {}, {}
    for i, s in enumerate(skus):
        for p, P in enumerate(PLANTS):
            v = x[ix['iN'](i, p)] * BATCH
            if v > 1e-6:
                prod[(s, P)] = v
            for h, Hh in enumerate(HUBS):
                v = x[ix['iPH'](i, p, h)]
                if v > 1e-6:
                    ph[(s, P, Hh)] = v
        for h, Hh in enumerate(HUBS):
            for c, C in enumerate(CFAS):
                v = x[ix['iHC'](i, h, c)]
                if v > 1e-6:
                    hc[(s, Hh, C)] = v
            if x[ix['iHS'](i, h)] > 1e-6:
                hubshort[(s, Hh)] = x[ix['iHS'](i, h)]
            if x[ix['iHE'](i, h)] > 1e-6:
                hubend[(s, Hh)] = x[ix['iHE'](i, h)]
        for c, C in enumerate(CFAS):
            if x[ix['iSH'](i, c)] > 1e-6:
                short[(s, C)] = x[ix['iSH'](i, c)]

    pc = sum(prod_cost[P] * v for (s, P), v in prod.items())
    t1 = sum(c_ph[(P, Hh)] * v for (s, P, Hh), v in ph.items())
    t2 = sum(c_hc[C][HUBS.index(Hh)] * v for (s, Hh, C), v in hc.items())
    pen = sum(d['penalty'][s] * (contract_mult if d['contractual'][s] else 1) * v
              for (s, C), v in short.items())
    hsp = sum(d['penalty'][s] * hub_pen_frac * v for (s, Hh), v in hubshort.items())
    hold = sum(hold_cost * v for v in hubend.values())
    tvc = tier_viol_cost or TIER_VIOL_COST
    tier_viol = {t: float(x[ix['iTV'](t)]) for t in TIERS if x[ix['iTV'](t)] > 1e-6}
    pol = sum(tvc[t] * v for t, v in tier_viol.items())
    demand = sum(req.values())
    unmet = sum(short.values())
    return dict(prod=prod, ph=ph, hc=hc, short=short, hubshort=hubshort, hubend=hubend,
                costs=dict(production=pc, freight_plant_hub=t1, freight_hub_cfa=t2,
                           penalty_unmet=pen, penalty_hub_ss=hsp, holding=hold,
                           policy_breach=pol,
                           total=pc + t1 + t2 + pen + hsp + hold + pol),
                tier_viol=tier_viol,
                produced_kl=sum(prod.values()), unmet_kl=unmet,
                fill_rate=(1 - unmet / demand) if demand > 0 else 1.0,
                hub_short_kl=sum(hubshort.values()),
                cap=cap, prod_cost=prod_cost, c_ph=c_ph, c_hc=c_hc,
                mip_gap=res.mip_gap if hasattr(res, 'mip_gap') else None)


def shadow_prices(d, req, hub_ss, cap=None, prod_cost=None, c_ph=None, c_hc=None,
                  contract_mult=3.0, hub_pen_frac=0.10, hold_cost=180.0,
                  tier_fill=None, tier_viol_cost=None):
    """LP-relaxation duals on plant x line capacity. Rs saved per extra kl/month.
       Valid for MARGINAL changes only."""
    cap = cap or DEFAULT_CAP
    prod_cost = prod_cost or DEFAULT_PROD_COST
    c_ph = c_ph or DEFAULT_C_PH
    c_hc = c_hc or DEFAULT_C_HC
    A, lo, hi, cost, integ, lb, ub, cap_rows, ix = _assemble(
        d, req, hub_ss, cap, prod_cost, c_ph, c_hc, contract_mult, hub_pen_frac,
        hold_cost, False, tier_fill, tier_viol_cost)
    eq = lo == hi
    ubr = ~eq
    fin = np.isfinite(hi[ubr])
    res = linprog(cost, A_ub=A[ubr][fin], b_ub=hi[ubr][fin], A_eq=A[eq], b_eq=lo[eq],
                  bounds=list(zip(lb, ub)), method='highs')
    if not res.success:
        return {}
    orig = np.where(ubr)[0][fin]
    pos = {o: k for k, o in enumerate(orig)}
    marg = res.ineqlin.marginals
    return {k: -float(marg[pos[r]]) for k, r in cap_rows.items() if r in pos}


def to_frames(d, r, norms):
    """Convert a solution into planner-readable tables."""
    prod = pd.DataFrame([{'SKU': s, 'Tier': d['tier'][s], 'Pack size': d['packsize'][s],
                          'Line': d['line'][s], 'Plant': P, 'Batches': int(round(v / BATCH)),
                          'Volume (kl)': round(v, 2),
                          'Cost (Rs)': round(v * r['prod_cost'][P])}
                         for (s, P), v in sorted(r['prod'].items())])
    ph = pd.DataFrame([{'SKU': s, 'Plant': P, 'Hub': H, 'Volume (kl)': round(v, 2),
                        'Freight (Rs)': round(v * r['c_ph'][(P, H)])}
                       for (s, P, H), v in sorted(r['ph'].items())])
    hc = pd.DataFrame([{'SKU': s, 'Hub': H, 'CFA': C, 'Volume (kl)': round(v, 2),
                        'Freight (Rs)': round(v * r['c_hc'][C][HUBS.index(H)])}
                       for (s, H, C), v in sorted(r['hc'].items())])
    unmet = pd.DataFrame([{'SKU': s, 'CFA': C, 'Tier': d['tier'][s],
                           'Contractual': 'YES' if d['contractual'][s] else 'No',
                           'Unmet (kl)': round(v, 3),
                           'Penalty (Rs)': round(v * d['penalty'][s] *
                                                 (3.0 if d['contractual'][s] else 1))}
                          for (s, C), v in sorted(r['short'].items(), key=lambda x: -x[1])])
    if unmet.empty:
        unmet = pd.DataFrame(columns=['SKU', 'CFA', 'Tier', 'Contractual',
                                      'Unmet (kl)', 'Penalty (Rs)'])
    used = {}
    for (s, P), v in r['prod'].items():
        used[(P, d['line'][s])] = used.get((P, d['line'][s]), 0) + v
    cap = pd.DataFrame([{'Plant': P, 'Line': L, 'Used (kl)': round(used.get((P, L), 0), 1),
                         'Capacity (kl)': r['cap'][P][L],
                         'Utilisation %': (round(100 * used.get((P, L), 0) / r['cap'][P][L], 1)
                                           if r['cap'][P][L] else None)}
                        for P in PLANTS for L in LINES])
    nrm = norms[['Product Name', 'Tier', 'CFA', 'hub', 'd_daily_adj', 'sig_d_adj',
                 'L', 'sigL', 'z', 'SS', 'ROP', 'DoC']].copy()
    nrm.columns = ['SKU', 'Tier', 'CFA', 'Hub', 'Daily demand (kl)', 'Sigma demand',
                   'Lead time (d)', 'Sigma LT (d)', 'z', 'Safety stock (kl)',
                   'Reorder point (kl)', 'Days of cover']
    for c in nrm.columns[4:]:
        nrm[c] = nrm[c].round(3)
    return dict(production=prod, plant_hub=ph, hub_cfa=hc, unmet=unmet,
                capacity=cap, norms=nrm)
