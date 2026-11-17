from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m7_reports.contracts import get_contract, validate_content
from app.modules.m7_reports.core import ReportService
from app.modules.m7_reports.receiver import LocalReceiver
from app.modules.m7_reports.repository import ReportRepository

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


@pytest.fixture
def svc(tmp_path):
    repo = ReportRepository(str(tmp_path / "reports.db"))
    return ReportService(repo, LocalReceiver(repo))


def _switch(svc, target, expected=1):
    return svc.switch_contract({"target_version": target, "expected_version": expected})


# ---- 契约 ---------------------------------------------------------------

def test_contract_registry_requires_branch_only_in_new(svc):
    assert get_contract(1)["requires_branch"] is False
    assert get_contract(2)["requires_branch"] is True


def test_switch_contract_bumps_generation(svc):
    before = svc.contract()
    after = _switch(svc, 2)
    assert before["active_version"] == 1
    assert after["active_version"] == 2
    assert after["generation"] == before["generation"] + 1


def test_switch_contract_rejects_stale_expected_version(svc):
    _switch(svc, 2)
    with pytest.raises(DomainError) as exc:
        _switch(svc, 1, expected=1)
    assert exc.value.code == "STALE_VERSION"
    assert exc.value.status == 409


def test_switch_contract_rejects_unknown_target(svc):
    with pytest.raises(DomainError) as exc:
        _switch(svc, 9)
    assert exc.value.code == "UNKNOWN_CONTRACT"


def test_validate_new_contract_rejects_missing_branch():
    content = {"approved": "10.00", "payees": [
        {"project_id": "A", "legal_entity": "甲法人", "weight": "1", "cap": "10.00"}]}
    with pytest.raises(DomainError) as exc:
        validate_content(content, 2)
    assert exc.value.code == "MISSING_BRANCH"


def test_validate_new_contract_rejects_missing_legal_entity():
    content = {"approved": "10.00", "payees": [
        {"project_id": "A", "branch": "甲分支", "weight": "1", "cap": "10.00"}]}
    with pytest.raises(DomainError) as exc:
        validate_content(content, 2)
    assert exc.value.code == "MISSING_LEGAL_ENTITY"


def test_legacy_contract_does_not_need_branch():
    normalized = validate_content(LEGACY, 1)
    assert normalized["approved"] == Decimal("1000.00")


# ---- 首次报送 -----------------------------------------------------------

def test_first_report_freezes_contract_and_content(svc):
    created = svc.create({"content": LEGACY})
    again = svc.create({"content": LEGACY})
    assert created["report_id"] == again["report_id"]
    assert again["note"] == "EXISTING"
    assert created["contract"]["version"] == 1
    assert created["content"] == LEGACY
    assert created["allocation_total"] == "1000.00"
    assert created["unallocated"] == "0.00"
    assert created["status"] == "PENDING"


def test_create_uses_existing_allocation_algorithm(svc):
    from app.modules.m4_allocations.core import allocate
    created = svc.create({"content": LEGACY})
    expected, left = allocate("1000.00", [
        {"id": "A", "weight": "5", "cap": "300.00"},
        {"id": "B", "weight": "3", "cap": "500.00"},
        {"id": "C", "weight": "2", "cap": "500.00"},
    ])
    assert created["allocation"] == {k: str(v) for k, v in expected.items()}
    assert Decimal(created["allocation_total"]) == sum(expected.values())
    assert Decimal(created["unallocated"]) == left


def test_same_report_id_with_new_content_rejected(svc):
    created = svc.create({"content": LEGACY, "report_id": "rpt-fixed"})
    changed = {"approved": "999.00", "payees": LEGACY["payees"]}
    with pytest.raises(DomainError) as exc:
        svc.create({"content": changed, "report_id": "rpt-fixed"})
    assert exc.value.code == "CONTENT_CONFLICT"
    assert exc.value.status == 409


def test_submit_with_conflicting_content_rejected(svc):
    created = svc.create({"content": LEGACY})
    with pytest.raises(DomainError) as exc:
        svc.submit(created["report_id"], {"content": {"approved": "1.00", "payees": []}})
    assert exc.value.code == "CONTENT_CONFLICT"


# ---- 切版与并发 ---------------------------------------------------------

def test_stale_contract_create_rejected_after_switch(svc):
    _switch(svc, 2)
    with pytest.raises(DomainError) as exc:
        svc.create({"content": LEGACY, "contract_version": 1})
    assert exc.value.code == "STALE_VERSION"
    assert exc.value.status == 409
    with pytest.raises(DomainError) as exc:
        svc.create({"content": LEGACY, "version": 1})
    assert exc.value.code == "STALE_VERSION"


def test_stale_report_insert_rejected_at_commit(svc, tmp_path):
    """模拟切版事务抢先提交：新建在写事务内复核版本，拒绝过期版本。"""
    from app.modules.m7_reports.repository import ReportRepository

    repo = svc.repo
    svc.create({"content": LEGACY, "report_id": "rpt-race"})
    svc.switch_contract({"target_version": 2, "expected_version": 1})
    record = {
        "report_id": "rpt-late", "contract_version": 1, "content_digest": "d",
        "content": LEGACY, "approved": "1000.00", "allocation": {},
        "allocation_total": "0.00", "unallocated": "0.00",
    }
    with pytest.raises(DomainError) as exc:
        repo.create_report(record, 1)
    assert exc.value.code == "STALE_VERSION"


def test_new_report_after_switch_uses_new_contract(svc):
    _switch(svc, 2)
    created = svc.create({"content": SPLIT})
    assert created["contract"]["version"] == 2
    assert created["contract"]["requires_branch"] is True


def test_switch_between_precheck_and_insert_rejected(svc, monkeypatch):
    """切版在新建预检之后、写事务之前提交：写事务内复核仍拒绝过期版本。"""
    real_state = svc.repo.contract_state
    monkeypatch.setattr(svc.repo, "contract_state", lambda: {"active_version": 1, "generation": 0})
    _switch(svc, 2)  # 真实状态已换到 v2
    with pytest.raises(DomainError) as exc:
        svc.create({"content": LEGACY})
    assert exc.value.code == "STALE_VERSION"
    monkeypatch.setattr(svc.repo, "contract_state", real_state)
    assert svc.contract()["active_version"] == 2


def test_concurrent_switch_and_create_never_mix_versions(svc):
    """切版与新建并发：要么旧版报送成功且切版成功，要么新建被拒；
    不会出现「已换到新版却插入旧版报送」。"""
    import threading

    outcomes = {}

    def do_create():
        try:
            outcomes["created"] = svc.create(
                {"content": LEGACY, "report_id": "rpt-concurrent", "contract_version": 1})
        except DomainError as exc:
            outcomes["create_error"] = exc.code

    def do_switch():
        try:
            outcomes["switched"] = svc.switch_contract({"target_version": 2, "expected_version": 1})
        except DomainError as exc:
            outcomes["switch_error"] = exc.code

    threads = [threading.Thread(target=do_create), threading.Thread(target=do_switch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    state = svc.contract()
    if "created" in outcomes:
        assert outcomes["created"]["contract"]["version"] == 1
        if state["active_version"] == 2:
            # 报送 froze v1，之后才换版；冻结的是当时仍有效的契约。
            assert outcomes["created"]["contract_generation"] == 0
    else:
        assert outcomes["create_error"] == "STALE_VERSION"
    assert state["active_version"] == 2


# ---- 回执与恢复 ---------------------------------------------------------

def test_submit_accepts_once(svc):
    created = svc.create({"content": LEGACY})
    first = svc.submit(created["report_id"])
    second = svc.submit(created["report_id"])
    assert first["status"] == "ACCEPTED"
    assert first["receipt"]["receipt_no"].startswith("RCP-")
    assert second["note"] == "ALREADY_ACCEPTED"
    assert svc.receiver_inbox()["accepted_count"] == 1


def test_receipt_lost_query_then_recover_without_duplicate(svc):
    created = svc.create({"content": LEGACY})
    lost = svc.submit(created["report_id"], {"simulate": "receipt_lost"})
    assert lost["status"] == "ACCEPTED_NO_RECEIPT"
    assert lost["receipt"] is None
    assert svc.receiver_inbox()["accepted_count"] == 1

    recovered = svc.submit(created["report_id"])
    assert recovered["status"] == "ACCEPTED"
    assert recovered["receipt"]["receipt_no"] == "RCP-" + created["content_digest"][:12]
    assert svc.receiver_inbox()["accepted_count"] == 1


def test_recovery_prefers_query_over_redelivery(svc):
    created = svc.create({"content": LEGACY})
    with pytest.raises(DomainError):
        svc.submit(created["report_id"], {"simulate": "accept_then_interrupt"})
    assert svc.receiver_inbox()["accepted_count"] == 1
    assert svc.read(created["report_id"])["status"] == "ACCEPTED_NO_RECEIPT"
    # 接收端已有回执：恢复只查询绑定，不重复收件。
    recovered = svc.recover(created["report_id"])
    assert recovered["status"] == "ACCEPTED"
    assert svc.receiver_inbox()["accepted_count"] == 1


def test_interrupt_leaves_resumable_state(svc):
    created = svc.create({"content": LEGACY})
    with pytest.raises(DomainError) as exc:
        svc.submit(created["report_id"], {"simulate": "interrupt"})
    assert exc.value.code == "SEND_INTERRUPTED"
    pending = svc.read(created["report_id"])
    assert pending["status"] == "PENDING"
    assert pending["last_error"] == "SEND_INTERRUPTED"
    assert svc.receiver_inbox()["accepted_count"] == 0

    resumed = svc.recover(created["report_id"])
    assert resumed["status"] == "ACCEPTED"
    assert svc.receiver_inbox()["accepted_count"] == 1


def test_receiver_unavailable_keeps_state(svc):
    created = svc.create({"content": LEGACY})
    svc.set_receiver_available(False)
    with pytest.raises(DomainError) as exc:
        svc.submit(created["report_id"])
    assert exc.value.code == "RECEIVER_UNAVAILABLE"
    assert svc.read(created["report_id"])["status"] == "PENDING"

    svc.set_receiver_available(True)
    assert svc.submit(created["report_id"])["status"] == "ACCEPTED"


def test_old_report_recovers_under_old_contract_after_switch(svc):
    created = svc.create({"content": LEGACY, "report_id": "rpt-old"})
    _switch(svc, 2)
    # 旧报送不接受新契约的校验，也不被补写分支字段。
    stored = svc.read(created["report_id"])
    assert stored["contract"]["version"] == 1
    assert "branch" not in stored["content"]["payees"][0]
    # 凭原内容重复首次报送：仍命中旧件，不改写、不按新契约重校验。
    again = svc.create({"content": LEGACY, "report_id": "rpt-old"})
    assert again["report_id"] == "rpt-old"
    assert again["contract"]["version"] == 1
    assert again["note"] == "EXISTING"
    recovered = svc.recover(created["report_id"])
    assert recovered["status"] == "ACCEPTED"
    assert recovered["content"] == LEGACY


def test_new_contract_report_rejects_missing_branch_at_receiver(svc):
    _switch(svc, 2)
    with pytest.raises(DomainError) as exc:
        svc.create({"content": LEGACY})
    assert exc.value.code == "MISSING_LEGAL_ENTITY"

    missing = {"approved": "10.00", "payees": [
        {"project_id": "A", "legal_entity": "甲法人", "weight": "1", "cap": "10.00"}]}
    with pytest.raises(DomainError) as exc:
        svc.receive({"report_id": "rpt-x", "contract_version": 2, "content": missing})
    assert exc.value.code == "MISSING_BRANCH"


def test_receiver_same_report_same_content_is_idempotent(svc):
    created = svc.create({"content": LEGACY})
    first = svc.receive({"report_id": created["report_id"], "contract_version": 1,
                         "content": LEGACY, "content_digest": created["content_digest"]})
    second = svc.receive({"report_id": created["report_id"], "contract_version": 1,
                          "content": LEGACY, "content_digest": created["content_digest"]})
    assert first["receipt"]["receipt_no"] == second["receipt"]["receipt_no"]
    inbox = svc.receiver_inbox()
    assert inbox["accepted_count"] == 1
    assert inbox["receipts"][0]["duplicate_deliveries"] == 1


def test_receiver_rejects_digest_mismatch(svc):
    with pytest.raises(DomainError) as exc:
        svc.receive({"report_id": "rpt-forged", "contract_version": 1,
                     "content": LEGACY, "content_digest": "0" * 64})
    assert exc.value.code == "CONTENT_CONFLICT"


def test_receiver_same_report_new_content_rejected(svc):
    created = svc.create({"content": LEGACY})
    svc.receive({"report_id": created["report_id"], "contract_version": 1,
                 "content": LEGACY, "content_digest": created["content_digest"]})
    changed = {"approved": "999.00", "payees": LEGACY["payees"]}
    with pytest.raises(DomainError) as exc:
        svc.receive({"report_id": created["report_id"], "contract_version": 1, "content": changed})
    assert exc.value.code == "CONTENT_CONFLICT"


def test_read_back_contract_content_receipt_and_total(svc):
    created = svc.create({"content": LEGACY})
    svc.submit(created["report_id"])
    view = svc.read(created["report_id"])
    assert view["contract"]["version"] == 1
    assert view["content"] == LEGACY
    assert view["receipt"]["report_id"] == created["report_id"]
    assert view["allocation_total"] == "1000.00"


def test_state_survives_new_service_instance(tmp_path):
    path = str(tmp_path / "shared.db")
    repo = ReportRepository(path)
    first = ReportService(repo, LocalReceiver(repo))
    created = first.create({"content": LEGACY})
    with pytest.raises(DomainError):
        first.submit(created["report_id"], {"simulate": "interrupt"})

    repo2 = ReportRepository(path)
    second = ReportService(repo2, LocalReceiver(repo2))
    assert second.read(created["report_id"])["status"] == "PENDING"
    assert second.recover(created["report_id"])["status"] == "ACCEPTED"
