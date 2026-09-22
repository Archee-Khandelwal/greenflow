"""
test_offline_sync.py - checks for the offline-sync side of scheduler_api.py: /ingest and /fleet.
Run with:  python test_offline_sync.py

These call the FastAPI route functions directly (FastAPI's @app.get/@app.post decorators return
the original function, so it stays a plain callable) rather than going through TestClient/httpx,
which are not project dependencies. Each test points the module at a throwaway fleet file so it
never touches the real fleet_log.json that already holds demo data.
"""
import os
import tempfile

import scheduler_api as api


def with_temp_fleet(fn):
    """Run fn() against an empty, temporary fleet store, then restore the real one untouched."""
    real_path, real_fleet = api._FLEET_PATH, api._fleet
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(tmp_path)  # _load_fleet treats a missing file as {}
    api._FLEET_PATH = tmp_path
    api._fleet = {}
    try:
        fn()
    finally:
        api._FLEET_PATH, api._fleet = real_path, real_fleet
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_ingest_stores_a_new_record():
    def run():
        res = api.ingest({"records": [{"id": "r-1", "workflow": "docs", "logged_at": "t", "summary": {"carbon_g": 100}}]})
        assert res["ok"] and res["stored_ids"] == ["r-1"] and res["already_had"] == [], res
        assert "r-1" in api._fleet
    with_temp_fleet(run)


def test_ingest_is_idempotent_by_id():
    """TEST 7: the same event id must never be counted twice, even sent in the same batch or resent later."""
    def run():
        rec = {"id": "r-dup", "workflow": "support", "logged_at": "t", "summary": {"carbon_g": 5}}
        first = api.ingest({"records": [rec]})
        assert first["stored_ids"] == ["r-dup"]
        second = api.ingest({"records": [rec]})  # simulates a client retry after a partial-failure response
        assert second["stored_ids"] == [] and second["already_had"] == ["r-dup"], second
        assert api._totals()["carbon_g"] == 5, "a duplicate resend must not double-count fleet totals"
    with_temp_fleet(run)


def test_ingest_batch_mixes_new_and_already_had():
    def run():
        api.ingest({"records": [{"id": "r-a", "workflow": "docs", "logged_at": "t", "summary": {"carbon_g": 1}}]})
        res = api.ingest({"records": [
            {"id": "r-a", "workflow": "docs", "logged_at": "t", "summary": {"carbon_g": 1}},  # already stored
            {"id": "r-b", "workflow": "docs", "logged_at": "t", "summary": {"carbon_g": 2}},   # new
        ]})
        assert set(res["stored_ids"]) == {"r-b"} and set(res["already_had"]) == {"r-a"}, res
    with_temp_fleet(run)


def test_ingest_rejects_records_without_a_usable_id():
    def run():
        res = api.ingest({"records": [{"workflow": "docs", "summary": {}}, {"id": 123, "summary": {}}]})
        assert res["ok"] and res["rejected"] == 2 and res["stored_ids"] == [], res
    with_temp_fleet(run)


def test_ingest_rejects_empty_batch():
    def run():
        res = api.ingest({"records": []})
        assert res.status_code == 422
    with_temp_fleet(run)


def test_fleet_totals_reconcile_across_records():
    def run():
        api.ingest({"records": [
            {"id": "r-1", "workflow": "docs", "logged_at": "t", "summary": {"carbon_g": 100, "cost_usd": 1, "energy_kwh": 0.5}},
            {"id": "r-2", "workflow": "docs", "logged_at": "t", "summary": {"carbon_g": 50, "cost_usd": 0.5, "energy_kwh": 0.25}},
        ]})
        f = api.fleet()
        assert f["run_count"] == 2
        assert f["totals"] == {"carbon_g": 150.0, "cost_usd": 1.5, "energy_kwh": 0.75}, f["totals"]
    with_temp_fleet(run)


def test_health_endpoint():
    assert api.health() == {"status": "ok"}


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
