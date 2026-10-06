import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import CorrectionItem, ErrataRequest, create_app, logic_submit_errata

SAMPLES = [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"},
]
CATEGORIES = ["cat", "dog", "bird"]

# reviewer1 gets everything right; reviewer2 flips S03
R1_LABELS = {"S01": "cat", "S02": "dog", "S03": "bird"}
R2_LABELS = {"S01": "cat", "S02": "dog", "S03": "cat"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_batch(client):
    r = client.post("/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES})
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, batch_id, token, labels):
    return client.post(
        f"/batches/{batch_id}/submissions",
        json={"labels": labels},
        headers={"X-Reviewer-Token": token},
    )


def seal_batch(client):
    """Create a batch and drive it to sealed (with one adjudicated disagreement)."""
    batch = make_batch(client)
    bid = batch["batch_id"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 201 and r.json()["phase"] == "sealed"
    return batch


def errata(client, batch_id, token, expected_revision, corrections):
    headers = {"X-Arbiter-Token": token} if token is not None else {}
    return client.post(
        f"/batches/{batch_id}/errata",
        json={
            "expected_revision": expected_revision,
            "corrections": corrections,
        },
        headers=headers,
    )


def test_errata_flow_and_versioned_views(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]

    # revision 0 is the sealed original: same labels and truth as /results
    original = client.get(f"/batches/{bid}/results").json()
    v0 = client.get(f"/batches/{bid}/errata/0").json()
    assert v0["revision"] == 0 and v0["latest_revision"] == 0
    assert v0["corrected"] == []
    strip = lambda rows: [{k: v for k, v in row.items() if k != "final_correct"} for row in rows]
    assert strip(v0["results"]) == original["results"]
    assert v0["summary"] == original["summary"]

    # S02 was recorded as dog but is really a cat
    r = errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["corrected"] == [
        {"code": "S02", "previous_true_label": "dog", "true_label": "cat"}
    ]

    # version 1: labels kept from the sealed original, truth recomputed
    v1 = client.get(f"/batches/{bid}/errata/1").json()
    assert v1["revision"] == 1 and v1["latest_revision"] == 1
    assert v1["corrected"] == [
        {"code": "S02", "previous_true_label": "dog", "true_label": "cat"}
    ]
    by_code = {row["code"]: row for row in v1["results"]}
    s02 = by_code["S02"]
    assert s02["true_label"] == "cat"          # corrected truth
    assert s02["final_label"] == "dog"         # original final label kept
    assert s02["reviewer1_label"] == "dog"     # original reviewer labels kept
    assert s02["reviewer2_label"] == "dog"
    assert s02["reviewer1_correct"] is False   # recomputed against the new truth
    assert s02["reviewer2_correct"] is False
    assert s02["final_correct"] is False
    assert by_code["S01"]["reviewer1_correct"] is True
    assert v1["summary"] == {
        "sample_count": 3,
        "disagreement_count": 1,
        "reviewer1_correct": 2,
        "reviewer2_correct": 1,
        "final_correct": 2,
    }

    # version 0 and the plain queries are untouched (only the latest pointer moved)
    v0_after = client.get(f"/batches/{bid}/errata/0").json()
    assert v0_after["latest_revision"] == 1
    assert {k: v for k, v in v0_after.items() if k != "latest_revision"} == {
        k: v for k, v in v0.items() if k != "latest_revision"
    }
    assert client.get(f"/batches/{bid}/results").json() == original
    assert client.get(f"/batches/{bid}").json() == original

    # revision beyond the latest does not exist
    assert client.get(f"/batches/{bid}/errata/2").status_code == 404


def test_errata_requires_arbiter_token(client):
    batch = seal_batch(client)
    bid = batch["batch_id"]
    corrections = [{"code": "S02", "true_label": "cat"}]

    assert errata(client, bid, None, 0, corrections).status_code == 401
    assert errata(client, bid, "nope", 0, corrections).status_code == 401
    # reviewer credentials cannot file errata
    assert errata(client, bid, batch["reviewer1_token"], 0, corrections).status_code == 401
    assert errata(client, bid, batch["reviewer2_token"], 0, corrections).status_code == 401
    # failed attempts created nothing
    assert client.get(f"/batches/{bid}/errata").json()["latest_revision"] == 0


def test_errata_only_after_sealing(client):
    batch = make_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    corrections = [{"code": "S02", "true_label": "cat"}]

    # collecting
    assert errata(client, bid, arb, 0, corrections).status_code == 409
    assert client.get(f"/batches/{bid}/errata").status_code == 409
    assert client.get(f"/batches/{bid}/errata/0").status_code == 409

    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    assert errata(client, bid, arb, 0, corrections).status_code == 409

    # adjudicating
    submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    assert errata(client, bid, arb, 0, corrections).status_code == 409


def test_errata_validation(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]

    # unknown sample code
    r = errata(client, bid, arb, 0, [{"code": "S99", "true_label": "cat"}])
    assert r.status_code == 422
    # duplicate code in the same request
    r = errata(
        client, bid, arb, 0,
        [{"code": "S02", "true_label": "cat"}, {"code": "S02", "true_label": "bird"}],
    )
    assert r.status_code == 422
    # corrected label must be an allowed category
    r = errata(client, bid, arb, 0, [{"code": "S02", "true_label": "fish"}])
    assert r.status_code == 422
    # no actual change (S01 is already cat)
    r = errata(client, bid, arb, 0, [{"code": "S01", "true_label": "cat"}])
    assert r.status_code == 422
    # empty corrections
    r = errata(client, bid, arb, 0, [])
    assert r.status_code == 422

    # no partial version was left behind by any of the failures
    listing = client.get(f"/batches/{bid}/errata").json()
    assert listing["latest_revision"] == 0
    assert [v["revision"] for v in listing["versions"]] == [0]
    assert client.get(f"/batches/{bid}/errata/1").status_code == 404


def test_stale_revision_returns_current(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]

    r = errata(client, bid, arb, 5, [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == 0

    assert errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}]).status_code == 201

    # replaying the same request is now stale
    r = errata(client, bid, arb, 0, [{"code": "S03", "true_label": "dog"}])
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == 1

    # following the reported revision succeeds
    r = errata(client, bid, arb, 1, [{"code": "S03", "true_label": "dog"}])
    assert r.status_code == 201 and r.json()["revision"] == 2


def test_revision_chain(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]

    r = errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 201
    # second erratum: flips S02 back and corrects S03 as well
    r = errata(
        client, bid, arb, 1,
        [{"code": "S02", "true_label": "dog"}, {"code": "S03", "true_label": "cat"}],
    )
    assert r.status_code == 201 and r.json()["revision"] == 2

    v2 = client.get(f"/batches/{bid}/errata/2").json()
    assert v2["latest_revision"] == 2
    # only this revision's own corrections are listed
    assert v2["corrected"] == [
        {"code": "S02", "previous_true_label": "cat", "true_label": "dog"},
        {"code": "S03", "previous_true_label": "bird", "true_label": "cat"},
    ]
    by_code = {row["code"]: row for row in v2["results"]}
    assert by_code["S02"]["true_label"] == "dog"   # back to the original truth
    assert by_code["S02"]["final_correct"] is True
    assert by_code["S03"]["true_label"] == "cat"
    assert by_code["S03"]["final_label"] == "bird"
    assert by_code["S03"]["final_correct"] is False
    assert v2["summary"]["final_correct"] == 2
    assert v2["summary"]["reviewer1_correct"] == 2
    assert v2["summary"]["reviewer2_correct"] == 3

    # earlier revisions still answer exactly as before
    v1 = client.get(f"/batches/{bid}/errata/1").json()
    assert {row["code"]: row["true_label"] for row in v1["results"]} == {
        "S01": "cat", "S02": "cat", "S03": "bird",
    }
    assert v1["corrected"] == [
        {"code": "S02", "previous_true_label": "dog", "true_label": "cat"}
    ]

    # the list endpoint shows the whole chain
    listing = client.get(f"/batches/{bid}/errata").json()
    assert listing["latest_revision"] == 2
    assert [v["revision"] for v in listing["versions"]] == [0, 1, 2]
    assert listing["versions"][2]["corrected"] == v2["corrected"]


def test_concurrent_errata_only_one_wins(tmp_path):
    """Concurrent corrections on the same base revision: exactly one applies."""
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = seal_batch(client)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        payload = ErrataRequest(
            expected_revision=0,
            corrections=[CorrectionItem(code="S02", true_label="cat")],
        )

        outcomes = []

        def worker():
            try:
                logic_submit_errata(store, bid, arb, payload)
                outcomes.append("ok")
            except HTTPException as exc:
                outcomes.append(exc.status_code)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("ok") == 1
        assert outcomes.count(409) == 7
        with store.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM errata WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
        assert n == 1
        assert client.get(f"/batches/{bid}/errata").json()["latest_revision"] == 1


def test_errata_persistence_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")

    app1 = create_app(db)
    with TestClient(app1) as client1:
        batch = seal_batch(client1)
        bid, arb = batch["batch_id"], batch["arbiter_token"]
        errata(client1, bid, arb, 0, [{"code": "S02", "true_label": "cat"}])
        errata(client1, bid, arb, 1, [{"code": "S03", "true_label": "dog"}])
        before = {
            rev: client1.get(f"/batches/{bid}/errata/{rev}").json() for rev in (0, 1, 2)
        }
        listing_before = client1.get(f"/batches/{bid}/errata").json()

    # "restart": brand-new app instance on the same database file
    app2 = create_app(db)
    with TestClient(app2) as client2:
        for rev in (0, 1, 2):
            assert client2.get(f"/batches/{bid}/errata/{rev}").json() == before[rev]
        assert client2.get(f"/batches/{bid}/errata").json() == listing_before


def test_errata_does_not_touch_sealed_original(client):
    batch = seal_batch(client)
    bid, arb = batch["batch_id"], batch["arbiter_token"]
    original_results = client.get(f"/batches/{bid}/results").json()
    original_batch = client.get(f"/batches/{bid}").json()
    own = client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]}
    ).json()

    assert errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}]).status_code == 201

    # the plain batch/results queries still return the sealed original
    assert client.get(f"/batches/{bid}/results").json() == original_results
    assert client.get(f"/batches/{bid}").json() == original_batch
    # submissions and adjudications are as final as before
    assert client.get(
        f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]}
    ).json() == own
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 409
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "cat"}},
        headers={"X-Arbiter-Token": arb},
    )
    assert r.status_code == 409


def test_errata_on_missing_batch(client):
    r = errata(client, "does-not-exist", "nope", 0, [{"code": "S01", "true_label": "cat"}])
    assert r.status_code == 404
    assert client.get("/batches/does-not-exist/errata").status_code == 404
    assert client.get("/batches/does-not-exist/errata/0").status_code == 404
