"""
scheduler_core.py - the carbon-aware workflow scheduler as a plain Python module (no dependencies).

It is a port of the engine inside the web app, and it gives the same numbers.
Give it a workflow description (a dict, or JSON) and it returns a plan plus a
"receipt" for every step: model, region, start time, energy, carbon, cost,
expected accuracy and the reasons for each decision.

    python scheduler_core.py                      # schedule the built-in document workflow
    python scheduler_core.py --workflow support   # or: docs, research, support
    python scheduler_core.py my_request.json      # schedule your own request
    python scheduler_core.py --json               # print the full JSON response

Everything is SIMULATED: carbon curves are synthetic unless you pass "carbonProfiles",
and energy, price and accuracy figures are assumptions unless you override them.
"""
import copy
import json
import math
import re
import sys
from decimal import Decimal, ROUND_HALF_UP

EPS = 1e-9


# ---------- small helpers that match the JavaScript behaviour ----------
def js_round(x):
    return math.floor(x + 0.5)


def to_fixed(x, d):
    q = Decimal(1).scaleb(-d)
    v = Decimal(abs(x)).quantize(q, rounding=ROUND_HALF_UP)
    return ("-" if x < 0 else "") + format(v, "f")


def round_to(x, d):
    f = 10 ** d
    return math.floor(x * f + 0.5) / f


def clamp(x, a, b):
    return max(a, min(b, x))


def fmt_c(g):
    if g < 1:
        return to_fixed(g * 1000, 0) + " mg"
    if g >= 10000:
        return to_fixed(g / 1000, 1) + " kg"
    if g >= 1000:
        return to_fixed(g / 1000, 2) + " kg"
    return to_fixed(g, 0) + " g"


def fmt_h(h):
    return re.sub(r"\.?0+$", "", to_fixed(h, 2)) + "h"


def pad2(n):
    return ("0" if n < 10 else "") + str(n)


def clock_of(cfg, t):
    tm = js_round((cfg["startIST"] + t) * 60)
    day = math.floor(tm / 1440)
    tm = ((tm % 1440) + 1440) % 1440
    return pad2(tm // 60) + ":" + pad2(tm % 60) + (" +%dd" % day if day > 0 else "")


# ---------- workflows ----------
def _st(id_, name, full, type_, deps, base_h, k_tok, min_acc, deadline, can_wait, data_gb, resident):
    return {"id": id_, "name": name, "full": full, "type": type_, "deps": deps, "baseH": base_h, "kTok": k_tok,
            "minAcc": min_acc, "deadline": deadline, "canWait": can_wait, "dataGB": data_gb, "resident": resident}


WORKFLOWS = {
    "docs": {"name": "Document compliance pipeline", "deadline": 18, "runsPerDay": 2, "hwShare": 1,
             "steps": [
                 _st("S1", "Ingest", "Ingest documents", "parse", [], 0.5, 500, 0.94, 2, False, 20, False),
                 _st("S2", "OCR", "OCR and layout parsing", "parse", ["S1"], 1.5, 4000, 0.94, 8, True, 20, False),
                 _st("S3", "Classify", "Classify document type", "classify", ["S2"], 1, 3000, 0.93, None, True, 2, False),
                 _st("S4", "Extract", "Extract fields", "extract", ["S2"], 2, 8000, 0.90, None, True, 4, False),
                 _st("S5", "Compliance", "Compliance check", "reason", ["S3", "S4"], 1.5, 6000, 0.90, None, True, 2, True),
                 _st("S6", "Report", "Write report", "summarize", ["S5"], 0.5, 2000, 0.85, None, False, 0.5, False)]},
    "research": {"name": "Research agent", "deadline": 12, "runsPerDay": 5, "hwShare": 1,
                 "steps": [
                     _st("S1", "Plan", "Plan the research", "plan", [], 0.25, 300, 0.85, 1, False, 0.1, False),
                     _st("S2", "Search", "Search sources", "search", ["S1"], 1, 2000, 0.90, None, True, 1, False),
                     _st("S3", "Papers", "Read and extract from papers", "extract", ["S2"], 2, 9000, 0.90, None, True, 6, False),
                     _st("S4", "News", "Read and extract from news", "extract", ["S2"], 1.5, 6000, 0.90, None, True, 3, False),
                     _st("S5", "Cross-check", "Cross-check claims", "reason", ["S3", "S4"], 1.5, 5000, 0.88, None, True, 1, False),
                     _st("S6", "Report", "Write report", "summarize", ["S5"], 1, 3000, 0.88, None, False, 0.5, False)]},
    "support": {"name": "Customer support ticket", "deadline": 0.75, "runsPerDay": 5000, "hwShare": 0.05,
                "steps": [
                    _st("S1", "Receive", "Receive and parse ticket", "parse", [], 0.05, 1, 0.94, None, False, 0.01, False),
                    _st("S2", "Intent", "Classify intent", "classify", ["S1"], 0.05, 2, 0.93, None, False, 0.01, False),
                    _st("S3", "Find", "Find answers in the knowledge base", "search", ["S2"], 0.1, 6, 0.90, None, False, 0.01, False),
                    _st("S4", "Draft", "Draft reply", "summarize", ["S3"], 0.2, 5, 0.85, None, False, 0.01, False),
                    _st("S5", "Policy", "Policy check", "reason", ["S4"], 0.1, 3, 0.90, None, False, 0.01, False),
                    _st("S6", "Send", "Send reply", "parse", ["S5"], 0.05, 0.5, 0.94, None, False, 0.01, False)]},
}


def apply_workflow(cfg, key):
    w = WORKFLOWS[key]
    cfg["workflow"] = key
    cfg["steps"] = copy.deepcopy(w["steps"])
    cfg["wfDeadline"] = w["deadline"]
    cfg["runsPerDay"] = w["runsPerDay"]
    cfg["hwShare"] = w.get("hwShare", 1)
    cfg["spike"]["on"] = False
    return cfg


def defaults():
    d = {
        "workflow": "docs", "home": "mumbai", "residencyRegion": "mumbai", "startIST": 20.5, "wfDeadline": 18,
        "forecastErr": 0, "noiseSeed": 0, "horizon": 48, "grid": 0.25, "transferKWhPerGB": 0.06,
        "runsPerDay": 2, "kgPerKm": 0.15, "hwShare": 1,
        "tiers": ["small", "medium", "large"],
        "models": {
            "small": {"name": "Small", "kwh": 0.0002, "price": 0.0004, "speed": 0.5, "emb": 20},
            "medium": {"name": "Medium", "kwh": 0.0008, "price": 0.002, "speed": 1, "emb": 60},
            "large": {"name": "Large", "kwh": 0.003, "price": 0.008, "speed": 2, "emb": 250},
        },
        "accuracy": {
            "parse": {"small": 0.95, "medium": 0.97, "large": 0.98},
            "classify": {"small": 0.90, "medium": 0.94, "large": 0.96},
            "extract": {"small": 0.82, "medium": 0.90, "large": 0.95},
            "reason": {"small": 0.62, "medium": 0.78, "large": 0.92},
            "summarize": {"small": 0.80, "medium": 0.88, "large": 0.93},
            "search": {"small": 0.88, "medium": 0.93, "large": 0.96},
            "plan": {"small": 0.70, "medium": 0.85, "large": 0.94},
        },
        "regions": [
            {"id": "mumbai", "name": "Mumbai", "tz": 5.5, "base": 660, "dip": 120, "dipHour": 13, "eve": 70, "eveHour": 20, "night": 0, "pue": 1.5, "priceMult": 1.0, "hPerGB": 0, "profile": None, "source": ""},
            {"id": "frankfurt", "name": "Frankfurt", "tz": 2, "base": 340, "dip": 100, "dipHour": 13, "eve": 90, "eveHour": 19, "night": 50, "pue": 1.3, "priceMult": 1.15, "hPerGB": 0.01, "profile": None, "source": ""},
            {"id": "oregon", "name": "Oregon", "tz": -7, "base": 200, "dip": 35, "dipHour": 13, "eve": 60, "eveHour": 19, "night": 0, "pue": 1.2, "priceMult": 1.0, "hPerGB": 0.02, "profile": None, "source": ""},
            {"id": "paris", "name": "Paris", "tz": 2, "base": 55, "dip": 8, "dipHour": 13, "eve": 12, "eveHour": 19, "night": 0, "pue": 1.3, "priceMult": 1.1, "hPerGB": 0.01, "profile": None, "source": ""},
        ],
        "steps": [],
        "spike": {"on": False, "region": "paris", "start": 6, "dur": 10, "mult": 6},
        "levers": {"time": True, "region": True, "model": True},
        "cascade": {"on": False, "catchRate": 0.8, "hardPenalty": 0.2, "verifier": 0.1},
    }
    apply_workflow(d, "docs")
    return d


def region_of(cfg, rid):
    for r in cfg["regions"]:
        if r["id"] == rid:
            return r
    return None


def eff_deadline(cfg, s):
    d = s["deadline"] if s["deadline"] is not None else math.inf
    return min(d, cfg["wfDeadline"])


def last_tier(cfg):
    return cfg["tiers"][-1]


def topo(steps):
    by_id = {s["id"]: s for s in steps}
    seen, out = set(), []

    def visit(s):
        if s is None or s["id"] in seen:
            return
        seen.add(s["id"])
        for d in s["deps"]:
            visit(by_id.get(d))
        out.append(s)

    for s in steps:
        visit(s)
    return out


# ---------- validation ----------
def validate(cfg):
    errs = []
    if not cfg.get("steps"):
        return ["The workflow has no steps."]
    if not (cfg["wfDeadline"] > 0):
        errs.append("The workflow deadline must be above 0 hours.")
    ids, by_id = set(), {}
    for s in cfg["steps"]:
        if s["id"] in ids:
            errs.append("Step id %s is used twice." % s["id"])
        ids.add(s["id"])
        by_id[s["id"]] = s

    def num_ok(x, test):
        try:
            return test(x)
        except TypeError:
            return False

    for s in cfg["steps"]:
        if not isinstance(s["deps"], list):
            errs.append("%s: deps must be a list of step ids." % s["id"])
            continue
        for d in s["deps"]:
            if d not in by_id:
                errs.append("%s depends on unknown step %s." % (s["id"], d))
        if s["type"] not in cfg["accuracy"]:
            errs.append('%s has unknown task type "%s". Use one of: %s.' % (s["id"], s["type"], ", ".join(cfg["accuracy"].keys())))
        if not num_ok(s["baseH"], lambda x: x > 0):
            errs.append("%s: hours must be above 0." % s["id"])
        if not num_ok(s["kTok"], lambda x: x >= 0):
            errs.append("%s: tokens must be 0 or more." % s["id"])
        if not num_ok(s["minAcc"], lambda x: 0 <= x <= 1):
            errs.append("%s: the accuracy floor must be between 0 and 1." % s["id"])
        if s["deadline"] is not None and not num_ok(s["deadline"], lambda x: x > 0):
            errs.append("%s: the deadline must be above 0 hours, or blank." % s["id"])
        if not num_ok(s["dataGB"], lambda x: x >= 0):
            errs.append("%s: data size must be 0 or more." % s["id"])
    if not errs:
        color, bad = {}, [None]

        def dfs(i):
            if color.get(i) == 1:
                bad[0] = i
                return True
            if color.get(i) == 2:
                return False
            color[i] = 1
            for d in by_id[i]["deps"]:
                if dfs(d):
                    return True
            color[i] = 2
            return False

        for s in cfg["steps"]:
            if dfs(s["id"]):
                break
        if bad[0]:
            errs.append("The workflow has a dependency cycle involving %s." % bad[0])
    return errs


# ---------- carbon model ----------
def _circ(x, c):
    d = abs(x - c) % 24
    return min(d, 24 - d)


def _gauss(x, c, w):
    d = _circ(x, c)
    return math.exp(-(d * d) / (2 * w * w))


def base_value(r, lh):
    p = r.get("profile")
    if p and len(p) == 24:
        x = lh - 0.5
        i0 = math.floor(x)
        f = x - i0
        a = p[int(((i0 % 24) + 24) % 24)]
        b = p[int((((i0 + 1) % 24) + 24) % 24)]
        return a * (1 - f) + b * f
    return r["base"] - r["dip"] * _gauss(lh, r["dipHour"], 3.5) - r["night"] * _gauss(lh, 3, 3) + r["eve"] * _gauss(lh, r["eveHour"], 2.2)


def daily_curve(r):
    return [max(10, base_value(r, h + 0.5)) for h in range(24)]


class CarbonCurves:
    """Average grid carbon intensity per region on a 0.05 h grid, with fast interval averages."""

    def __init__(self, cfg, noise, spike):
        self.R = 0.05
        self.N = int(math.ceil((cfg["horizon"] + 40) / self.R))
        start_utc = (((cfg["startIST"] - 5.5) % 24) + 24) % 24
        self.pref = {}
        for idx, r in enumerate(cfg["regions"]):
            pref = [0.0] * (self.N + 1)
            for k in range(self.N):
                t = (k + 0.5) * self.R
                utc = start_utc + t
                lh = (((utc + r["tz"]) % 24) + 24) % 24
                v = base_value(r, lh)
                if noise and cfg["forecastErr"] > 0:
                    sd = cfg.get("noiseSeed", 0) or 0
                    v *= 1 + cfg["forecastErr"] * (0.6 * math.sin(2 * math.pi * t / 17 + idx * 1.7 + sd * 2.3) + 0.4 * math.sin(2 * math.pi * t / 5.3 + idx * 0.9 + sd * 1.1))
                sp = cfg.get("spike")
                if spike and sp and sp["on"] and sp["region"] == r["id"] and sp["start"] <= t < sp["start"] + sp["dur"]:
                    v *= sp["mult"]
                pref[k + 1] = pref[k] + max(10, v)
            self.pref[r["id"]] = pref

    def avg(self, rid, s, d):
        p = self.pref[rid]
        a = clamp(js_round(s / self.R), 0, self.N - 1)
        b = clamp(js_round((s + d) / self.R), a + 1, self.N)
        return (p[b] - p[a]) / (b - a)


# ---------- options ----------
def chain_stats(cfg, s, chain):
    acc0 = cfg["accuracy"][s["type"]]
    if len(chain) == 1:
        return {"P": [1], "acc": acc0[chain[0]]}
    c = cfg["cascade"]
    P, acc, Ps = 1.0, 0.0, []
    for j, t in enumerate(chain):
        aj = acc0[t] * (1 if j == 0 else (1 - c["hardPenalty"]))
        Ps.append(P)
        acc += P * aj
        if j < len(chain) - 1:
            P *= (1 - aj) * c["catchRate"]
    return {"P": Ps, "acc": acc}


def option_info(cfg, s, tier, region, chain=None):
    chain = chain if chain else [tier]
    stt = chain_stats(cfg, s, chain)
    remote = region["id"] != cfg["home"]
    ver = cfg["cascade"]["verifier"] if len(chain) > 1 else 0
    transfer_h = s["dataGB"] * region["hPerGB"] if remote else 0
    compute_h = kwh = cost = emb_g = 0.0
    hw = 1 if cfg.get("hwShare") is None else cfg["hwShare"]
    for j, t in enumerate(chain):
        m = cfg["models"][t]
        P = stt["P"][j]
        vf = ver if j < len(chain) - 1 else 0
        h = s["baseH"] * m["speed"]
        compute_h += P * h
        kwh += P * s["kTok"] * m["kwh"] * region["pue"] * (1 + vf)
        cost += P * s["kTok"] * m["price"] * region["priceMult"] * (1 + vf)
        emb_g += P * m["emb"] * h * hw
    return {"tier": chain[0], "chain": chain, "stageP": stt["P"], "region": region["id"], "computeH": compute_h,
            "transferH": transfer_h, "dur": compute_h + transfer_h, "kwh": kwh,
            "trKwh": s["dataGB"] * cfg["transferKWhPerGB"] if remote else 0, "cost": cost, "acc": stt["acc"], "embG": emb_g}


def carbon_of(cfg, ci, o, start):
    ci_r = ci.avg(o["region"], start, o["dur"])
    op = o["kwh"] * ci_r
    tr = 0.0
    if o["trKwh"] > 0:
        tr = o["trKwh"] * (ci.avg(cfg["home"], start, o["dur"]) + ci_r) / 2
    emb = o["embG"]
    return {"op": op, "tr": tr, "emb": emb, "total": op + tr + emb, "ciR": ci_r}


def allowed_regions(cfg, s):
    return [r for r in cfg["regions"] if not s["resident"] or r["id"] == cfg["residencyRegion"]]


def home_for(cfg, s):
    a = allowed_regions(cfg, s)
    for r in a:
        if r["id"] == cfg["home"]:
            return r
    return a[0]


def feasible_options(cfg, s, lev=None):
    lev = lev or cfg["levers"]
    tiers = [t for t in cfg["tiers"] if cfg["accuracy"][s["type"]][t] >= s["minAcc"] - EPS]
    if not tiers:
        tiers = [last_tier(cfg)]
    if not lev["model"]:
        tiers = [last_tier(cfg)]
    regs = allowed_regions(cfg, s)
    if not lev["region"]:
        h = [r for r in regs if r["id"] == cfg["home"]]
        if h:
            regs = h
    out = [option_info(cfg, s, t, r) for t in tiers for r in regs]
    if cfg.get("cascade") and cfg["cascade"]["on"] and lev["model"]:
        T = cfg["tiers"]
        L = len(T)
        for mask in range(1, 1 << (L - 1)):
            chain = [T[i] for i in range(L - 1) if mask & (1 << i)]
            chain.append(T[L - 1])
            if chain_stats(cfg, s, chain)["acc"] < s["minAcc"] - EPS:
                continue
            for r in regs:
                out.append(option_info(cfg, s, chain[0], r, chain))
    return out


def backward(cfg, order, dmin):
    LF, LS = {}, {}
    for s in reversed(order):
        lf = eff_deadline(cfg, s)
        for t in order:
            if s["id"] in t["deps"]:
                lf = min(lf, LS[t["id"]])
        LF[s["id"]] = lf
        LS[s["id"]] = lf - dmin[s["id"]]
    return LF, LS


# ---------- the scheduler ----------
def plan_ours(cfg, ci, levers=None, frozen=None, t0=0.0):
    lev = levers or cfg["levers"]
    order = topo(cfg["steps"])
    opts, dmin = {}, {}
    for s in order:
        opts[s["id"]] = feasible_options(cfg, s, lev)
        dmin[s["id"]] = min(o["dur"] for o in opts[s["id"]])
    LF, LS = backward(cfg, order, dmin)
    plan, end, meta = {}, {}, {}
    for s in order:
        es = 0.0
        for d in s["deps"]:
            es = max(es, end[d])
        if frozen and s["id"] in frozen:
            f = frozen[s["id"]]
            plan[s["id"]] = f
            end[s["id"]] = f["start"] + option_info(cfg, s, f["tier"], region_of(cfg, f["region"]), f.get("chain"))["dur"]
            meta[s["id"]] = {"ES": es, "LF": LF[s["id"]], "LS": LS[s["id"]], "dmin": dmin[s["id"]], "late": False, "frozen": True}
            continue
        es = max(es, t0)
        shift = lev["time"] and s["canWait"]
        best = None
        for op in opts[s["id"]]:
            if not shift and es + op["dur"] > LF[s["id"]] + EPS:
                continue
            last_start = min(LF[s["id"]] - op["dur"], cfg["horizon"]) if shift else es
            n = math.floor((last_start - es) / cfg["grid"] + EPS)
            for k in range(0, n + 1):
                stt = es + k * cfg["grid"]
                c = carbon_of(cfg, ci, op, stt)["total"] + cfg.get("wk", 0) * op["cost"] + cfg.get("wt", 0) * (stt + op["dur"])
                if best is None or c < best["c"] - 1e-9 or (abs(c - best["c"]) <= 1e-9 and stt + op["dur"] < best["st"] + best["op"]["dur"] - 1e-9):
                    best = {"c": c, "op": op, "st": stt}
        late = False
        if best is None:
            bo = None
            for op in opts[s["id"]]:
                if bo is None or op["dur"] < bo["dur"]:
                    bo = op
            best = {"c": 0, "op": bo, "st": es}
            late = True
        plan[s["id"]] = {"tier": best["op"]["tier"], "chain": best["op"]["chain"], "region": best["op"]["region"], "start": best["st"]}
        end[s["id"]] = best["st"] + best["op"]["dur"]
        meta[s["id"]] = {"ES": es, "LF": LF[s["id"]], "LS": LS[s["id"]], "dmin": dmin[s["id"]], "late": late, "frozen": False}
    return {"plan": plan, "meta": meta, "LF": LF, "LS": LS, "dmin": dmin}


def plan_largest(cfg):
    order = topo(cfg["steps"])
    plan, end = {}, {}
    for s in order:
        es = 0.0
        for d in s["deps"]:
            es = max(es, end[d])
        plan[s["id"]] = {"tier": last_tier(cfg), "region": home_for(cfg, s)["id"], "start": es}
        end[s["id"]] = es + option_info(cfg, s, last_tier(cfg), region_of(cfg, plan[s["id"]]["region"]))["dur"]
    return plan


def evaluate(cfg, ci, plan):
    order = topo(cfg["steps"])
    rows, end, issues = [], {}, []
    tot = {"op": 0.0, "tr": 0.0, "emb": 0.0, "carbon": 0.0, "kwh": 0.0, "cost": 0.0}
    acc, acc_sum, misses, makespan = 1.0, 0.0, 0, 0.0
    for s in order:
        p = plan[s["id"]]
        r = region_of(cfg, p["region"])
        o = option_info(cfg, s, p["tier"], r, p.get("chain"))
        c = carbon_of(cfg, ci, o, p["start"])
        e = p["start"] + o["dur"]
        end[s["id"]] = e
        ready = 0.0
        for d in s["deps"]:
            ready = max(ready, end[d])
        if p["start"] < ready - 1e-6:
            issues.append(s["name"] + " starts before its inputs are ready")
        if o["acc"] < s["minAcc"] - 1e-9:
            issues.append(s["name"] + " is below its accuracy floor")
        if s["resident"] and p["region"] != cfg["residencyRegion"]:
            issues.append(s["name"] + " breaks the residency rule")
        if not s["canWait"] and p["start"] > ready + 1e-6:
            issues.append(s["name"] + " was delayed but is marked cannot wait")
        late = e > eff_deadline(cfg, s) + 1e-6
        if late:
            misses += 1
        acc *= o["acc"]
        acc_sum += o["acc"]
        makespan = max(makespan, e)
        tot["op"] += c["op"]; tot["tr"] += c["tr"]; tot["emb"] += c["emb"]; tot["carbon"] += c["total"]
        tot["kwh"] += o["kwh"] + o["trKwh"]; tot["cost"] += o["cost"]
        rows.append({"id": s["id"], "name": s["name"], "full": s["full"], "tier": p["tier"], "region": p["region"], "start": p["start"],
                     "end": e, "dur": o["dur"], "carbon": c, "acc": o["acc"], "late": late, "opt": o})
    return {"rows": rows, "total": tot, "acc": acc, "accMean": acc_sum / (len(rows) or 1), "misses": misses,
            "makespan": makespan, "issues": issues}


def lower_bound(cfg, ci):
    """No schedule can emit less than this: each step picks its best option and start time on its own,
    ignoring step order. The plan's gap to this number bounds how far it is from the true optimum."""
    tot = 0.0
    for s in cfg["steps"]:
        lf, best = eff_deadline(cfg, s), math.inf
        for o in feasible_options(cfg, s):
            t = 0.0
            while t + o["dur"] <= lf + EPS and t <= cfg["horizon"]:
                best = min(best, carbon_of(cfg, ci, o, t)["total"])
                t += cfg["grid"]
        tot += 0.0 if best == math.inf else best
    return tot


# ---------- reasons ----------
def explain_step(cfg, ci, pr, row):
    s = next(x for x in cfg["steps"] if x["id"] == row["id"])
    m = pr["meta"][row["id"]]
    out = []
    home = region_of(cfg, cfg["home"])

    def mn(t):
        return cfg["models"][t]["name"]

    def rn(i):
        return region_of(cfg, i)["name"]

    chain = row["opt"]["chain"]
    rej = [(t, cfg["accuracy"][s["type"]][t]) for t in cfg["tiers"] if cfg["accuracy"][s["type"]][t] < s["minAcc"] - 1e-9]
    if len(chain) > 1:
        parts = ", ".join("%s handles %d%% of items" % (mn(t), js_round(row["opt"]["stageP"][j] * 100)) for j, t in enumerate(chain))
        out.append(("Model", "Cascade %s: %s. Expected accuracy %s against the %s floor. The escalated first attempts and the verifier work are counted in the carbon and time shown. This depends on the cascade assumptions (verifier catch rate %s, escalated-item penalty %s)."
                    % (" → ".join(mn(t) for t in chain), parts, to_fixed(row["acc"], 3), to_fixed(s["minAcc"], 2), _num(cfg["cascade"]["catchRate"]), _num(cfg["cascade"]["hardPenalty"]))))
    else:
        if rej:
            head = " and ".join("%s (%s)" % (mn(t), to_fixed(a, 2)) for t, a in rej) + (" falls" if len(rej) == 1 else " fall") + " below the %s accuracy floor. " % to_fixed(s["minAcc"], 2)
        else:
            head = "Every size meets the %s accuracy floor. " % to_fixed(s["minAcc"], 2)
        out.append(("Model", head + "%s (%s) is the %s" % (mn(row["tier"]), to_fixed(row["acc"], 2), "lowest-carbon size that passes." if cfg["levers"]["model"] else "size used.")))
    if s["resident"]:
        out.append(("Region", "Restricted to %s by the data residency rule." % rn(cfg["residencyRegion"])))
    elif row["region"] == cfg["home"]:
        alt = None
        for r in cfg["regions"]:
            if r["id"] == cfg["home"]:
                continue
            c = carbon_of(cfg, ci, option_info(cfg, s, row["tier"], r, row["opt"]["chain"]), row["start"])["total"]
            if alt is None or c < alt[0]:
                alt = (c, r)
        mine = carbon_of(cfg, ci, row["opt"], row["start"])["total"]
        tail = ""
        if alt:
            tail = "The best other region (%s) would emit %s %s after counting the %s GB transfer%s" % (
                alt[1]["name"], fmt_c(abs(alt[0] - mine)), "more" if alt[0] >= mine else "less", _num(s["dataGB"]),
                "." if alt[0] >= mine else ", but it does not fit the deadlines.")
        out.append(("Region", "Stays in %s. %s" % (rn(cfg["home"]), tail)))
    else:
        hc = carbon_of(cfg, ci, option_info(cfg, s, row["tier"], home, row["opt"]["chain"]), row["start"])
        mc = carbon_of(cfg, ci, row["opt"], row["start"])
        out.append(("Region", "Runs in %s: the grid averages %s g/kWh during the run against %s at home. After the %s GB transfer (%s extra) it saves %s (%d%%) against running here at home."
                    % (rn(row["region"]), to_fixed(mc["ciR"], 0), to_fixed(hc["ciR"], 0), _num(s["dataGB"]), fmt_h(row["opt"]["transferH"]),
                       fmt_c(hc["total"] - mc["total"]), js_round((1 - mc["total"] / hc["total"]) * 100))))
    slack = m["LS"] - m["ES"]
    if not s["canWait"]:
        out.append(("Timing", 'Marked "cannot wait": starts as soon as its inputs are ready (%s).' % clock_of(cfg, row["start"])))
    elif not cfg["levers"]["time"]:
        out.append(("Timing", "Time shifting is switched off."))
    else:
        delay = row["start"] - m["ES"]
        at_es = carbon_of(cfg, ci, row["opt"], m["ES"])
        at_st = carbon_of(cfg, ci, row["opt"], row["start"])
        if delay < 0.01:
            out.append(("Timing", "Starts at the earliest time. No later window that still meets the deadline is cleaner. Slack was %s." % fmt_h(max(0, slack))))
        else:
            out.append(("Timing", "Waits %s: the grid is %s g/kWh at the chosen start against %s at the earliest start, saving %s. Slack was %s, and it finishes %s before its latest finish."
                        % (fmt_h(delay), to_fixed(at_st["ciR"], 0), to_fixed(at_es["ciR"], 0), fmt_c(at_es["total"] - at_st["total"]), fmt_h(slack), fmt_h(max(0, m["LF"] - row["end"])))))
    if m["late"]:
        out.append(("Deadline", "Even the fastest option cannot meet this deadline."))
    if m.get("frozen"):
        out.append(("Re-plan", "Already started when the grid spike hit, so it kept its plan."))
    return out


def _num(x):
    """Format a number the way JavaScript prints it (0.8, 20, 0.05)."""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return repr(x) if isinstance(x, float) else str(x)


# ---------- spec in, receipts out ----------
def _deep_merge(t, s):
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(t.get(k), dict):
            _deep_merge(t[k], v)
        else:
            t[k] = v
    return t


def from_spec(spec):
    cfg = defaults()
    spec = spec or {}
    w = spec.get("workflow")
    if w:
        if isinstance(w.get("steps"), list):
            steps = []
            for i, x in enumerate(w["steps"]):
                sid = x.get("id") or "S%d" % (i + 1)
                nm = x.get("name") or x.get("id") or "S%d" % (i + 1)
                steps.append({"id": sid, "name": nm, "full": x.get("label") or nm, "type": x.get("type"),
                              "deps": [] if x.get("deps") is None else x["deps"],
                              "baseH": _f(x.get("baseHours")), "kTok": _f(x.get("kTokens")), "minAcc": _f(x.get("minAccuracy")),
                              "deadline": None if x.get("deadlineHours") is None else _f(x["deadlineHours"]),
                              "canWait": True if x.get("canWait") is None else bool(x["canWait"]),
                              "dataGB": 0 if x.get("dataGB") is None else _f(x["dataGB"]), "resident": bool(x.get("indiaOnly"))})
            cfg["steps"] = steps
        if w.get("deadlineHours") is not None:
            cfg["wfDeadline"] = _f(w["deadlineHours"])
        if w.get("name"):
            cfg["workflowName"] = w["name"]
    if spec.get("submitAtIST"):
        p = str(spec["submitAtIST"]).split(":")
        cfg["startIST"] = int(p[0]) + int(p[1] if len(p) > 1 and p[1] else 0) / 60
    if spec.get("forecastError") is not None:
        cfg["forecastErr"] = _f(spec["forecastError"])
    if spec.get("hardwareShare") is not None:
        cfg["hwShare"] = _f(spec["hardwareShare"])
    c = spec.get("cascades")
    if c:
        cfg["cascade"]["on"] = bool(c.get("enabled"))
        if c.get("catchRate") is not None:
            cfg["cascade"]["catchRate"] = _f(c["catchRate"])
        if c.get("hardPenalty") is not None:
            cfg["cascade"]["hardPenalty"] = _f(c["hardPenalty"])
        if c.get("verifierEnergyShare") is not None:
            cfg["cascade"]["verifier"] = _f(c["verifierEnergyShare"])
    for rid, v in (spec.get("carbonProfiles") or {}).items():
        r = region_of(cfg, rid)
        if r and isinstance(v, list) and len(v) == 24:
            try:
                r["profile"] = [float(x) for x in v]
                r["source"] = (spec.get("carbonSources") or {}).get(rid) or "supplied"
            except (TypeError, ValueError):
                pass
    if spec.get("valueOfSpeed") is not None:
        cfg["wt"] = _f(spec["valueOfSpeed"])  # grams CO2e you would pay to finish one hour sooner
    if spec.get("valueOfCost") is not None:
        cfg["wk"] = _f(spec["valueOfCost"])  # grams CO2e you would pay to save one dollar
    g = spec.get("gridSpike")
    if g:
        cfg["spike"] = {"on": True, "region": g.get("region", "paris"), "start": _f(g.get("startHours", 6)),
                        "dur": _f(g.get("durationHours", 10)), "mult": _f(g.get("multiplier", 6))}
    if spec.get("overrides"):
        _deep_merge(cfg, spec["overrides"])
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def to_spec(cfg):
    t = cfg["startIST"]
    hh = math.floor(t)
    mm = js_round((t - hh) * 60)
    return {"workflow": {"name": cfg.get("workflowName") or WORKFLOWS[cfg["workflow"]]["name"], "deadlineHours": cfg["wfDeadline"],
                         "steps": [{"id": s["id"], "name": s["name"], "label": s["full"], "type": s["type"], "deps": list(s["deps"]),
                                    "baseHours": s["baseH"], "kTokens": s["kTok"], "minAccuracy": s["minAcc"], "deadlineHours": s["deadline"],
                                    "canWait": s["canWait"], "dataGB": s["dataGB"], "indiaOnly": s["resident"]} for s in cfg["steps"]]},
            "submitAtIST": pad2(hh) + ":" + pad2(mm), "forecastError": cfg["forecastErr"], "hardwareShare": cfg.get("hwShare", 1),
            "cascades": {"enabled": bool(cfg["cascade"]["on"]), "catchRate": cfg["cascade"]["catchRate"],
                         "hardPenalty": cfg["cascade"]["hardPenalty"], "verifierEnergyShare": cfg["cascade"]["verifier"]}}


def example_spec(workflow="docs"):
    cfg = defaults()
    apply_workflow(cfg, workflow)
    return to_spec(cfg)


def schedule(spec):
    """Take a request (dict) and return {'ok': True, 'summary': ..., 'receipts': [...]} or {'ok': False, 'errors': [...]}."""
    try:
        cfg = from_spec(spec)
    except Exception as e:  # malformed request
        return {"ok": False, "errors": ["Could not read the request: %s" % e]}
    errs = validate(cfg)
    if errs:
        return {"ok": False, "errors": errs}
    sp = cfg["spike"] if cfg["spike"]["on"] else None
    ci_plan = CarbonCurves(cfg, True, False)
    ci_true = CarbonCurves(cfg, False, True)
    pr = plan_ours(cfg, ci_plan)
    ev = evaluate(cfg, ci_true, pr["plan"])
    spike_info = None
    if sp:  # the grid spike is unknown when the plan is made; re-plan the steps that have not started yet
        ev0 = ev
        frozen = {k: v for k, v in pr["plan"].items() if v["start"] < sp["start"] - EPS}
        ci_plan = CarbonCurves(cfg, True, True)
        pr = plan_ours(cfg, ci_plan, None, frozen, sp["start"])
        ev = evaluate(cfg, ci_true, pr["plan"])
        moved = [b["name"] for a, b in zip(ev0["rows"], ev["rows"])
                 if a["region"] != b["region"] or abs(a["start"] - b["start"]) > 0.01 or a["opt"]["chain"] != b["opt"]["chain"]]
        spike_info = {"region": sp["region"], "start_hours": sp["start"], "duration_hours": sp["dur"], "multiplier": sp["mult"],
                      "original_plan_g": round_to(ev0["total"]["carbon"], 3), "replanned_g": round_to(ev["total"]["carbon"], 3), "steps_moved": moved}
    base = evaluate(cfg, ci_true, plan_largest(cfg))
    lb = lower_bound(cfg, ci_true)
    weighted = bool(cfg.get("wt")) or bool(cfg.get("wk"))
    receipts = []
    for row in ev["rows"]:
        receipts.append({
            "step": row["id"], "name": row["full"],
            "model_chain": [cfg["models"][t]["name"] for t in row["opt"]["chain"]],
            "region": region_of(cfg, row["region"])["name"],
            "start_hours": round_to(row["start"], 3), "end_hours": round_to(row["end"], 3),
            "start_ist": clock_of(cfg, row["start"]), "end_ist": clock_of(cfg, row["end"]),
            "grid_g_per_kwh": round_to(row["carbon"]["ciR"], 1),
            "energy_kwh": round_to(row["opt"]["kwh"] + row["opt"]["trKwh"], 4),
            "carbon_g": {"operational": round_to(row["carbon"]["op"], 3), "transfer": round_to(row["carbon"]["tr"], 3),
                         "hardware": round_to(row["carbon"]["emb"], 3), "total": round_to(row["carbon"]["total"], 3)},
            "cost_usd": round_to(row["opt"]["cost"], 4),
            "expected_accuracy": round_to(row["acc"], 4),
            "deadline_met": not row["late"],
            "reasons": [k + ": " + t for k, t in explain_step(cfg, ci_plan, pr, row)],
        })
    return {"ok": True,
            "summary": {"total_carbon_g": round_to(ev["total"]["carbon"], 3), "baseline_always_largest_g": round_to(base["total"]["carbon"], 3),
                        "saving_percent": round_to(100 * (1 - ev["total"]["carbon"] / base["total"]["carbon"]), 2),
                        "finish_hours": round_to(ev["makespan"], 3), "late_steps": ev["misses"], "rule_breaks": len(ev["issues"]),
                        "estimated_accuracy_all_steps_right": round_to(ev["acc"], 4), "average_step_accuracy": round_to(ev["accMean"], 4),
                        "energy_kwh": round_to(ev["total"]["kwh"], 4), "cost_usd": round_to(ev["total"]["cost"], 3),
                        "lower_bound_g": round_to(lb, 3),
                        "gap_to_lower_bound_percent": None if weighted or lb <= 0 else round_to(100 * (ev["total"]["carbon"] / lb - 1), 2)},
            "receipts": receipts,
            "spike": spike_info,
            "notes": ["Carbon curves are synthetic unless carbonProfiles are supplied.",
                      "Energy, price and accuracy figures are assumptions unless overridden.",
                      "Carbon uses average, not marginal, grid intensity."]}


def main(argv):
    workflow, path, as_json = "docs", None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--workflow":
            workflow = argv[i + 1]
            i += 1
        elif a == "--json":
            as_json = True
        else:
            path = a
        i += 1
    if path:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    else:
        spec = example_spec(workflow)
    res = schedule(spec)
    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["ok"] else 1
    if not res["ok"]:
        print("The request was rejected:")
        for e in res["errors"]:
            print(" -", e)
        return 1
    s = res["summary"]
    print("Carbon %s vs %s for always-largest (%s%% lower); finishes in %s h; %d late step(s)."
          % (fmt_c(s["total_carbon_g"]), fmt_c(s["baseline_always_largest_g"]), s["saving_percent"], s["finish_hours"], s["late_steps"]))
    for r in res["receipts"]:
        print("  %-3s %-32s %-22s %-10s %s -> %s  %s" % (r["step"], r["name"][:32], " > ".join(r["model_chain"]), r["region"], r["start_ist"], r["end_ist"], fmt_c(r["carbon_g"]["total"])))
    print("Simulated results: see the notes in the JSON output (--json).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
