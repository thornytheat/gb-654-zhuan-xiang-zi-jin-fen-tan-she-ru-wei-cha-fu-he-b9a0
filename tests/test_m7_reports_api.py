LEGACY = {
    "approved": "1000.00",
    "payees": [
        {"project_id": "A", "payee": "甲单位（含分支）", "amount": "300.00", "weight": "5", "cap": "300.00"},
        {"project_id": "B", "payee": "乙单位", "amount": "420.00", "weight": "3", "cap": "500.00"},
        {"project_id": "C", "payee": "丙单位", "amount": "280.00", "weight": "2", "cap": "500.00"},
    ],
}

SPLIT = {
    "approved": "1000.00",
    "payees": [
        {"project_id": "A", "legal_entity": "甲法人", "branch": "甲分支", "weight": "5", "cap": "300.00"},
        {"project_id": "B", "legal_entity": "乙法人", "branch": "乙分支", "weight": "3", "cap": "500.00"},
        {"project_id": "C", "legal_entity": "丙法人", "branch": "丙分支", "weight": "2", "cap": "500.00"},
    ],
}


def test_reports_page(client):
    response = client.get("/reports")
    assert response.status_code == 200
    assert "财政报送记录" in response.text


def test_contract_read_back(client):
    body = client.get("/api/reports/contract").json()
    assert body["active_version"] == 1
    assert body["contract"]["requires_branch"] is False
    assert [c["version"] for c in body["registered"]] == [1, 2]


def test_switch_then_stale_create_conflicts(client):
    switched = client.post("/api/reports/contract/switch",
                           json={"target_version": 2, "expected_version": 1})
    assert switched.status_code == 200
    assert switched.json()["active_version"] == 2

    stale = client.post("/api/reports", json={"content": LEGACY, "contract_version": 1})
    assert stale.status_code == 409
    assert stale.json()["error"] == "STALE_VERSION"

    repeated = client.post("/api/reports/contract/switch",
                           json={"target_version": 1, "expected_version": 1})
    assert repeated.status_code == 409


def test_new_contract_missing_branch_rejected(client):
    client.post("/api/reports/contract/switch", json={"target_version": 2, "expected_version": 1})
    missing = {"approved": "10.00", "payees": [
        {"project_id": "A", "legal_entity": "甲法人", "weight": "1", "cap": "10.00"}]}
    response = client.post("/api/reports", json={"content": missing})
    assert response.status_code == 422
    assert response.json()["error"] == "MISSING_BRANCH"


def test_report_lifecycle_over_http(client):
    created = client.post("/api/reports", json={"content": LEGACY, "report_id": "rpt-http"}).json()
    assert created["contract"]["version"] == 1
    assert created["allocation_total"] == "1000.00"

    lost = client.post("/api/reports/rpt-http/submit", json={"simulate": "receipt_lost"}).json()
    assert lost["status"] == "ACCEPTED_NO_RECEIPT"
    assert client.get("/api/receiver/inbox").json()["accepted_count"] == 1

    recovered = client.post("/api/reports/rpt-http/recover").json()
    assert recovered["status"] == "ACCEPTED"
    assert recovered["receipt"]["receipt_no"].startswith("RCP-")
    assert client.get("/api/receiver/inbox").json()["accepted_count"] == 1

    read = client.get("/api/reports/rpt-http").json()
    assert read["content"] == LEGACY
    assert read["allocation_total"] == "1000.00"
    assert read["receipt"]["report_id"] == "rpt-http"


def test_same_report_changed_content_rejected_over_http(client):
    client.post("/api/reports", json={"content": LEGACY, "report_id": "rpt-fixed"})
    changed = {"approved": "1.00", "payees": LEGACY["payees"]}
    response = client.post("/api/reports", json={"content": changed, "report_id": "rpt-fixed"})
    assert response.status_code == 409
    assert response.json()["error"] == "CONTENT_CONFLICT"


def test_receiver_intake_is_idempotent_over_http(client):
    payload = {"report_id": "rpt-replay", "contract_version": 1, "content": LEGACY}
    first = client.post("/api/receiver/intake", json=payload).json()
    second = client.post("/api/receiver/intake", json=payload).json()
    assert first["receipt"]["receipt_no"] == second["receipt"]["receipt_no"]
    assert client.get("/api/receiver/inbox").json()["accepted_count"] == 1


def test_old_report_recovered_with_original_content_after_switch(client):
    created = client.post("/api/reports", json={"content": LEGACY}).json()
    client.post("/api/reports/contract/switch", json={"target_version": 2, "expected_version": 1})
    recovered = client.post(f"/api/reports/{created['report_id']}/recover").json()
    assert recovered["contract"]["version"] == 1
    assert recovered["content"] == LEGACY
    assert "branch" not in recovered["content"]["payees"][0]


def test_missing_report_returns_404(client):
    assert client.get("/api/reports/rpt-missing").status_code == 404
