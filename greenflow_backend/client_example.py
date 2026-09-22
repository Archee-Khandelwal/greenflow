"""
client_example.py - how an agent framework would use the scheduler.

The agent asks the scheduler for a plan BEFORE running its workflow, then runs each step
with the model, region and start time it was given. Here the "run" is only printed, so you
can see the shape of the integration without any real LLM calls.

    python client_example.py                 # uses the service at http://127.0.0.1:8000
    python client_example.py --local         # no service needed, calls the Python module directly
    python client_example.py --workflow support
"""
import json
import sys
import urllib.error
import urllib.request

import scheduler_core as core

URL = "http://127.0.0.1:8000/schedule"


def get_plan(spec, local):
    if local:
        return core.schedule(spec)
    req = urllib.request.Request(URL, data=json.dumps(spec).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 422 = the request was rejected; the body says why
        return json.loads(e.read().decode("utf-8"))
    except urllib.error.URLError:
        print("Could not reach the service at", URL, "- start it with: python scheduler_api.py (or use --local)")
        sys.exit(1)


def run_step(receipt):
    """Stand-in for 'call the LLM'. Replace this with your own call using the chosen model and region."""
    print("  run %-3s with %-22s in %-10s at %s (simulated %s g CO2e)"
          % (receipt["step"], " > ".join(receipt["model_chain"]), receipt["region"], receipt["start_ist"], receipt["carbon_g"]["total"]))


def main(argv):
    local = "--local" in argv
    workflow = argv[argv.index("--workflow") + 1] if "--workflow" in argv else "docs"
    spec = core.example_spec(workflow)
    plan = get_plan(spec, local)
    if not plan.get("ok"):
        print("The scheduler rejected the request:")
        for e in plan.get("errors", []):
            print(" -", e)
        return 1
    s = plan["summary"]
    print("Plan received: %s g CO2e, %s%% below always-largest, finishes in %s h, %d late step(s)."
          % (s["total_carbon_g"], s["saving_percent"], s["finish_hours"], s["late_steps"]))
    for receipt in sorted(plan["receipts"], key=lambda r: r["start_hours"]):
        run_step(receipt)
    print("Reasons for the last step:")
    for line in plan["receipts"][-1]["reasons"]:
        print("  -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
