"""
test_scheduler.py - checks for the scheduler. Run with:  python test_scheduler.py
Each test has an answer you can verify on paper.
"""
import copy
import math

import scheduler_core as core


def make_test_cfg():
    def step(i, deps, base_h, k_tok):
        return {"id": i, "name": i, "full": i, "type": "t", "deps": deps, "baseH": base_h, "kTok": k_tok, "minAcc": 0.9,
                "deadline": None, "canWait": True, "dataGB": 0, "resident": False}
    cfg = core.defaults()
    cfg.update({"home": "r1", "residencyRegion": "r1", "startIST": 5.5, "wfDeadline": 8, "tiers": ["medium"],
                "models": {"medium": {"name": "Medium", "kwh": 0.001, "price": 0.002, "speed": 1, "emb": 60}},
                "accuracy": {"t": {"medium": 0.95}}, "hwShare": 1,
                "regions": [{"id": "r1", "name": "R1", "tz": 0, "base": 100, "dip": 0, "dipHour": 13, "eve": 0, "eveHour": 19,
                             "night": 0, "pue": 1, "priceMult": 1, "hPerGB": 0, "profile": None, "source": ""}],
                "steps": [step("A", [], 1, 1000), step("B", ["A"], 2, 500), step("D", ["A"], 1, 500), step("C", ["B", "D"], 1, 500)],
                "cascade": {"on": False, "catchRate": 1, "hardPenalty": 0, "verifier": 0}})
    return cfg


def close(a, b):
    return abs(a - b) < 1e-6


def test_slack():
    cfg = make_test_cfg()
    ci = core.CarbonCurves(cfg, False, False)
    pr = core.plan_ours(cfg, ci)
    slack = {s["id"]: pr["LS"][s["id"]] - pr["meta"][s["id"]]["ES"] for s in cfg["steps"]}
    assert close(slack["A"], 4) and close(slack["B"], 4) and close(slack["C"], 4), slack
    assert close(slack["D"], 5), slack


def test_carbon_of_step_a():
    cfg = make_test_cfg()
    ci = core.CarbonCurves(cfg, False, False)
    a = cfg["steps"][0]
    o = core.option_info(cfg, a, "medium", cfg["regions"][0])
    c = core.carbon_of(cfg, ci, o, 0)
    assert close(c["total"], 160), c  # 1 kWh x 100 g/kWh + 60 g hardware share


def test_flat_grid_no_delay():
    cfg = make_test_cfg()
    ci = core.CarbonCurves(cfg, False, False)
    pr = core.plan_ours(cfg, ci)
    starts = [pr["plan"][k]["start"] for k in "ABDC"]
    assert starts == [0, 1, 1, 3], starts


def test_cascade_numbers():
    cfg = make_test_cfg()
    cfg["tiers"] = ["small", "large"]
    cfg["models"] = {"small": {"name": "Small", "kwh": 0.001, "price": 0.002, "speed": 1, "emb": 20},
                     "large": {"name": "Large", "kwh": 0.004, "price": 0.008, "speed": 2, "emb": 100}}
    cfg["accuracy"] = {"t": {"small": 0.8, "large": 0.95}}
    cfg["steps"] = [cfg["steps"][0]]
    cfg["cascade"] = {"on": True, "catchRate": 1, "hardPenalty": 0, "verifier": 0}
    ci = core.CarbonCurves(cfg, False, False)
    o = core.option_info(cfg, cfg["steps"][0], "small", cfg["regions"][0], ["small", "large"])
    assert close(o["kwh"], 1.8) and close(o["acc"], 0.99), o
    assert close(core.carbon_of(cfg, ci, o, 0)["total"], 240)
    pr = core.plan_ours(cfg, ci)
    assert len(pr["plan"]["A"]["chain"]) == 2  # the cascade (240 g) beats Large alone (600 g)


def test_validation():
    spec = core.example_spec()
    bad = copy.deepcopy(spec); bad["workflow"]["steps"][1]["deps"] = ["S9"]
    assert not core.schedule(bad)["ok"]
    cyc = copy.deepcopy(spec); cyc["workflow"]["steps"][0]["deps"] = ["S6"]
    assert not core.schedule(cyc)["ok"]
    typ = copy.deepcopy(spec); typ["workflow"]["steps"][2]["type"] = "nope"
    assert not core.schedule(typ)["ok"]


def test_all_workflows_feasible():
    for key in core.WORKFLOWS:
        res = core.schedule(core.example_spec(key))
        assert res["ok"] and res["summary"]["late_steps"] == 0 and res["summary"]["rule_breaks"] == 0, (key, res.get("summary"))


def test_known_default_result():
    res = core.schedule(core.example_spec("docs"))
    assert abs(res["summary"]["total_carbon_g"] - 17123.752) < 0.01, res["summary"]  # same number as the web app


def test_cascade_total_matches_web_app():
    spec = core.example_spec("docs"); spec["cascades"]["enabled"] = True
    assert abs(core.schedule(spec)["summary"]["total_carbon_g"] - 8305.809) < 0.01


def test_lower_bound():
    s = core.schedule(core.example_spec("docs"))["summary"]
    assert s["lower_bound_g"] <= s["total_carbon_g"] + 1e-6 and s["gap_to_lower_bound_percent"] < 1, s


def test_grid_spike_replan():
    spec = core.example_spec("docs"); spec["gridSpike"] = {"region": "paris", "startHours": 6, "durationHours": 10, "multiplier": 6}
    sp = core.schedule(spec)["spike"]
    assert abs(sp["original_plan_g"] - 20684.355) < 0.01 and abs(sp["replanned_g"] - 18864.666) < 0.01, sp


def test_value_of_speed():
    spec = core.example_spec("docs"); spec["valueOfSpeed"] = 800
    s = core.schedule(spec)["summary"]
    assert abs(s["finish_hours"] - 6.75) < 0.01 and 20500 < s["total_carbon_g"] < 20700, s


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, e)
    print("%d of %d passed" % (len(tests) - failed, len(tests)))
    raise SystemExit(1 if failed else 0)
