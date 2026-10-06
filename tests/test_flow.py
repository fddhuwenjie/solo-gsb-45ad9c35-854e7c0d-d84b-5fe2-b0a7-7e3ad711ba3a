import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app, logic_submit

SAMPLES = [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"},
]
CATEGORIES = ["cat", "dog", "bird"]


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


# reviewer1 gets everything right; reviewer2 flips S03
R1_LABELS = {"S01": "cat", "S02": "dog", "S03": "bird"}
R2_LABELS = {"S01": "cat", "S02": "dog", "S03": "cat"}


def test_create_batch_credentials_and_blind_view(client):
    batch = make_batch(client)
    for key in ("reviewer1_token", "reviewer2_token", "arbiter_token"):
        assert batch[key] and isinstance(batch[key], str)
    assert len({batch["reviewer1_token"], batch["reviewer2_token"], batch["arbiter_token"]}) == 3

    r = client.get(f"/batches/{batch['batch_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "collecting"
    assert body["samples"] == ["S01", "S02", "S03"]
    # blind phase: no truth, no labels, no credentials anywhere in the payload
    assert "true_label" not in r.text
    assert "token" not in r.text

    r = client.get("/batches/does-not-exist")
    assert r.status_code == 404


def test_full_flow_with_disagreement(client):
    batch = make_batch(client)
    bid = batch["batch_id"]

    # arbiter cannot peek before both submissions are in
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]})
    assert r.status_code == 409

    r = submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    assert r.status_code == 201 and r.json()["phase"] == "collecting"

    # still blind for the public and for the arbiter
    assert client.get(f"/batches/{bid}").json()["phase"] == "collecting"
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]})
    assert r.status_code == 409
    assert client.get(f"/batches/{bid}/results").status_code == 409

    r = submit(client, bid, batch["reviewer2_token"], R2_LABELS)
    assert r.status_code == 201 and r.json()["phase"] == "adjudicating"

    # late submissions are rejected
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 409

    # arbiter sees exactly the disagreement with both labels, nothing else
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]})
    assert r.status_code == 200
    assert r.json()["disagreements"] == [
        {"code": "S03", "reviewer1_label": "bird", "reviewer2_label": "cat"}
    ]
    assert "true_label" not in r.text

    # partial adjudication rejected
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 422
    # label not among the two reviewers' choices rejected
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "dog"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 422
    # batch is still adjudicating after the failed attempts
    assert client.get(f"/batches/{bid}").json()["phase"] == "adjudicating"

    # complete adjudication seals the batch
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 201 and r.json()["phase"] == "sealed"

    # adjudications are final
    r = client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "cat"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    assert r.status_code == 409

    # unblinded results
    r = client.get(f"/batches/{bid}/results")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {
        "sample_count": 3,
        "disagreement_count": 1,
        "reviewer1_correct": 3,
        "reviewer2_correct": 2,
        "final_correct": 3,
    }
    by_code = {row["code"]: row for row in body["results"]}
    assert by_code["S01"]["agreed"] is True
    assert by_code["S03"]["agreed"] is False
    assert by_code["S03"]["final_label"] == "bird"
    assert by_code["S03"]["true_label"] == "bird"
    assert by_code["S03"]["reviewer1_label"] == "bird"
    assert by_code["S03"]["reviewer2_label"] == "cat"

    # the plain batch query now returns the same unblinded payload
    assert client.get(f"/batches/{bid}").json() == body
    # repeated reads are identical (no diverging results)
    assert client.get(f"/batches/{bid}/results").json() == body


def test_no_disagreement_seals_on_second_submission(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).json()["phase"] == "collecting"
    r = submit(client, bid, batch["reviewer2_token"], dict(R1_LABELS))
    assert r.status_code == 201 and r.json()["phase"] == "sealed"

    body = client.get(f"/batches/{bid}/results").json()
    assert body["summary"]["disagreement_count"] == 0
    assert body["summary"]["reviewer1_correct"] == 3
    assert body["summary"]["reviewer2_correct"] == 3
    assert all(row["final_label"] == row["true_label"] for row in body["results"])

    # arbiter has nothing left to do
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]})
    assert r.status_code == 409


def test_duplicate_submission_does_not_overwrite(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    assert submit(client, bid, batch["reviewer1_token"], R1_LABELS).status_code == 201

    changed = {"S01": "dog", "S02": "cat", "S03": "dog"}
    r = submit(client, bid, batch["reviewer1_token"], changed)
    assert r.status_code == 409

    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]})
    assert r.status_code == 200
    assert r.json()["labels"] == R1_LABELS  # original submission intact


def test_reviewer_sees_only_own_status(client):
    batch = make_batch(client)
    bid = batch["batch_id"]

    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer2_token"]})
    assert r.json() == {"batch_id": bid, "reviewer": 2, "submitted": False}

    submit(client, bid, batch["reviewer1_token"], R1_LABELS)
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer1_token"]})
    body = r.json()
    assert body["submitted"] is True and body["labels"] == R1_LABELS
    assert "true_label" not in r.text  # never leaks truth
    # reviewer2 still sees only their own (empty) status
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["reviewer2_token"]})
    assert r.json()["submitted"] is False


def test_tokens_are_role_bound(client):
    batch = make_batch(client)
    bid = batch["batch_id"]

    # missing / bogus tokens
    assert client.get(f"/batches/{bid}/submission").status_code == 401
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": "nope"})
    assert r.status_code == 401
    # arbiter token is not a reviewer token
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": batch["arbiter_token"]})
    assert r.status_code == 401
    # reviewer token is not the arbiter token
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["reviewer1_token"]})
    assert r.status_code == 401
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": "nope"})
    assert r.status_code == 401


def test_submission_validation(client):
    batch = make_batch(client)
    bid = batch["batch_id"]
    token = batch["reviewer1_token"]

    assert submit(client, bid, token, {"S01": "cat", "S02": "dog"}).status_code == 422  # missing
    extra = dict(R1_LABELS, S99="cat")
    assert submit(client, bid, token, extra).status_code == 422  # unknown code
    bad = dict(R1_LABELS, S01="fish")
    assert submit(client, bid, token, bad).status_code == 422  # label not allowed
    # failed attempts did not record anything
    r = client.get(f"/batches/{bid}/submission", headers={"X-Reviewer-Token": token})
    assert r.json()["submitted"] is False


def test_batch_creation_validation(client):
    base = {"allowed_categories": CATEGORIES, "samples": SAMPLES}
    assert client.post("/batches", json={**base, "allowed_categories": []}).status_code == 422
    assert client.post("/batches", json={**base, "allowed_categories": ["cat", "cat"]}).status_code == 422
    assert client.post("/batches", json={**base, "samples": []}).status_code == 422
    dup = [SAMPLES[0], SAMPLES[0]]
    assert client.post("/batches", json={**base, "samples": dup}).status_code == 422
    bad = [{"code": "S01", "true_label": "fish"}]
    assert client.post("/batches", json={**base, "samples": bad}).status_code == 422


def test_concurrent_duplicate_submission_only_one_wins(tmp_path):
    """Interleaved duplicate submissions: exactly one is recorded."""
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        batch = make_batch(client)
        bid, token = batch["batch_id"], batch["reviewer1_token"]

        outcomes = []

        def worker():
            try:
                logic_submit(store, bid, token, R1_LABELS)
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
                "SELECT COUNT(*) AS c FROM submissions WHERE batch_id = ?", (bid,)
            ).fetchone()["c"]
        assert n == 1


def test_interleaved_requests_do_not_unblind_early(client):
    """While only one submission is in, no endpoint reveals anything."""
    batch = make_batch(client)
    bid = batch["batch_id"]
    submit(client, bid, batch["reviewer1_token"], R1_LABELS)

    assert client.get(f"/batches/{bid}/results").status_code == 409
    r = client.get(f"/batches/{bid}/disagreements", headers={"X-Arbiter-Token": batch["arbiter_token"]})
    assert r.status_code == 409
    public = client.get(f"/batches/{bid}").json()
    assert set(public) == {"batch_id", "phase", "samples"}
    assert public["phase"] == "collecting"


def test_persistence_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")

    app1 = create_app(db)
    with TestClient(app1) as client1:
        batch = make_batch(client1)
        bid = batch["batch_id"]
        submit(client1, bid, batch["reviewer1_token"], R1_LABELS)
        submit(client1, bid, batch["reviewer2_token"], R2_LABELS)
        client1.post(
            f"/batches/{bid}/adjudications",
            json={"decisions": {"S03": "bird"}},
            headers={"X-Arbiter-Token": batch["arbiter_token"]},
        )
        before = client1.get(f"/batches/{bid}/results").json()

    # "restart": brand-new app instance on the same database file
    app2 = create_app(db)
    with TestClient(app2) as client2:
        after = client2.get(f"/batches/{bid}/results").json()
        assert after == before
        assert after["summary"]["reviewer1_correct"] == 3
        assert after["summary"]["reviewer2_correct"] == 2
