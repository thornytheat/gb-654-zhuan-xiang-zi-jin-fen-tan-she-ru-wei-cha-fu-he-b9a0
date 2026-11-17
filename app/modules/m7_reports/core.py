"""M7 财政报送契约换版与本地接收端。

生命周期：

1. 首次报送时在同一事务内固定「契约版本 + 报送原文」，并用既有分摊算法
   算出分摊结果；此后原文与契约都不可改写。
2. 发送前状态先落库。发送中断后可从库存的 PENDING 状态恢复。
3. 回执丢失时先向本地接收端查询：查到就绑定回执，不再重复投递；
   确实未收件才按原内容重试，接收端按 report_id 幂等收件。
4. 同一 report_id 更换原文一律拒绝；换版不回溯、不补写旧报送。
"""
from __future__ import annotations

from decimal import Decimal

from app.errors import DomainError
from app.manifest import canonical_digest
from app.modules.m4_allocations.core import allocate

from .contracts import all_contracts, get_contract, validate_content
from .receiver import LocalReceiver
from .repository import STATUS_ACCEPTED, ReportRepository

CENT = Decimal("0.01")
SIMULATE_RECEIPT_LOST = "receipt_lost"
SIMULATE_INTERRUPT = "interrupt"
SIMULATE_ACCEPT_THEN_INTERRUPT = "accept_then_interrupt"


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(CENT))


class ReportService:
    def __init__(self, repository: ReportRepository, receiver: LocalReceiver | None = None):
        self.repo = repository
        self.receiver = receiver or LocalReceiver(repository)

    # ---- 契约 ---------------------------------------------------------

    def contract(self) -> dict:
        state = self.repo.contract_state()
        return {**state, "contract": get_contract(state["active_version"]),
                "registered": all_contracts()}

    def switch_contract(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("EMPTY_VALUE", "换版请求必须是对象")
        target = payload.get("target_version", payload.get("version"))
        if target is None:
            raise DomainError("EMPTY_VALUE", "换版请求缺少目标版本")
        get_contract(target)  # 未登记的目标版本直接拒绝
        state = self.repo.switch_contract(target, payload.get("expected_version"))
        return {**state, "contract": get_contract(state["active_version"])}

    # ---- 报送 ---------------------------------------------------------

    def create(self, payload: dict) -> dict:
        """首次报送：固定契约版本与原文，并计算分摊。"""
        if not isinstance(payload, dict):
            raise DomainError("EMPTY_VALUE", "报送请求必须是对象")
        content = payload.get("content")
        if content is None:
            raise DomainError("EMPTY_VALUE", "报送请求缺少原文")

        requested_id = payload.get("report_id")
        if requested_id is not None and not str(requested_id).strip():
            raise DomainError("EMPTY_VALUE", "报送编号不能为空")
        report_id = str(requested_id).strip() if requested_id is not None else None

        # 已有报送优先按冻结契约比对原文：换版后凭原内容重复首次报送，
        # 返回原报送而不是用新契约去校验旧原文。
        if report_id is not None:
            existing = self.repo.find_report(report_id)
            if existing is not None:
                frozen_digest = canonical_digest(
                    {"contract_version": existing["contract_version"], "content": content}
                )
                if frozen_digest != existing["content_digest"]:
                    raise DomainError(
                        "CONTENT_CONFLICT",
                        f"报送 {report_id} 已固定为另一份内容，拒绝改写",
                        status=409,
                    )
                return self._view(existing, note="EXISTING")

        state = self.repo.contract_state()
        requested = payload.get("contract_version",
                                payload.get("expected_version", payload.get("version")))
        if requested is None:
            version = state["active_version"]
        else:
            version = get_contract(requested)["version"]
            if version != state["active_version"]:
                raise DomainError(
                    "STALE_VERSION",
                    f"报送契约版本 {version} 已过期，当前为 {state['active_version']}",
                    status=409,
                )

        normalized = validate_content(content, version)
        approved = normalized["approved"]
        items = [{"id": p["project_id"], "weight": p["weight"], "cap": p["cap"]}
                 for p in normalized["payees"]]
        allocated, left = allocate(approved, items)

        digest = canonical_digest({"contract_version": version, "content": content})
        if report_id is None:
            report_id = "rpt-" + digest[:12]
        existing = self.repo.find_report(report_id)
        if existing is not None:
            if existing["content_digest"] != digest:
                raise DomainError(
                    "CONTENT_CONFLICT",
                    f"报送 {report_id} 已固定为另一份内容，拒绝改写",
                    status=409,
                )
            return self._view(existing, note="EXISTING")

        record = {
            "report_id": report_id,
            "contract_version": version,
            "content_digest": digest,
            "content": content,
            "approved": _money(approved),
            "allocation": {key: _money(value) for key, value in allocated.items()},
            "allocation_total": _money(sum(allocated.values(), Decimal("0"))),
            "unallocated": _money(left),
        }
        return self._view(self.repo.create_report(record, version), note="CREATED")

    # ---- 发送 / 恢复 --------------------------------------------------

    def _find_receipt(self, report_id: str) -> dict | None:
        return self.receiver.query(report_id)

    def _intake(self, report: dict) -> dict:
        return self.receiver.intake(
            report["report_id"],
            report["contract_version"],
            report["content"],
            report["content_digest"],
        )

    def _deliver(self, report: dict) -> dict:
        return self.repo.bind_receipt(report["report_id"], self._intake(report))

    def submit(self, report_id: str, payload: dict | None = None) -> dict:
        """发送一份已冻结的报送。

        任何情况下都不会用新内容改写已冻结原文；同一 report_id 只对应一份内容。
        """
        payload = payload or {}
        report = self.repo.get_report(report_id)
        self._guard_content(report, payload.get("content"))
        simulate = payload.get("simulate")

        if report["status"] == STATUS_ACCEPTED and report["receipt"]:
            return self._view(report, note="ALREADY_ACCEPTED")

        # 回执丢失或上次发送中断：先查询本地接收端，已有回执就只绑定，不重投。
        found = self._find_receipt(report_id)
        if found:
            report = self.repo.bind_receipt(report_id, found)
            return self._view(report, note="RECEIPT_RECOVERED")

        if simulate in (SIMULATE_INTERRUPT, SIMULATE_ACCEPT_THEN_INTERRUPT):
            self.repo.mark_attempt(report_id, "SEND_INTERRUPTED")
            if simulate == SIMULATE_ACCEPT_THEN_INTERRUPT:
                # 接收端其实已收件，但发送端在写回执前中断。
                self._intake(report)
                self.repo.mark_accepted_without_receipt(report_id)
            raise DomainError("SEND_INTERRUPTED", "发送中断，状态已保存，可恢复", status=503)

        if simulate == SIMULATE_RECEIPT_LOST:
            # 接收端已受理，回执在返回途中丢失。
            self.repo.mark_attempt(report_id)
            self._intake(report)
            report = self.repo.mark_accepted_without_receipt(report_id)
            return self._view(report, note="RECEIPT_LOST")

        self.repo.mark_attempt(report_id)
        try:
            report = self._deliver(report)
        except DomainError as exc:
            if exc.code == "RECEIPT_LOST":
                report = self.repo.mark_accepted_without_receipt(report_id)
                return self._view(report, note="RECEIPT_LOST")
            raise
        return self._view(report, note="ACCEPTED")

    def recover(self, report_id: str) -> dict:
        """从已存状态恢复报送。

        旧报送按首次报送时冻结的契约与原文复核：不补新栏、不改写原文。
        回执已存在则只绑定回执；未收件才按原内容继续投递。
        """
        report = self.repo.get_report(report_id)
        validate_content(report["content"], report["contract_version"])
        if report["status"] == STATUS_ACCEPTED and report["receipt"]:
            return self._view(report, note="ALREADY_ACCEPTED")
        return self.submit(report_id, {})

    @staticmethod
    def _guard_content(report: dict, content) -> None:
        if content is None:
            return
        digest = canonical_digest({"contract_version": report["contract_version"], "content": content})
        if digest != report["content_digest"]:
            raise DomainError(
                "CONTENT_CONFLICT",
                f"报送 {report['report_id']} 已固定为另一份内容，拒绝改写",
                status=409,
            )

    # ---- 回读 ---------------------------------------------------------

    def _view(self, report: dict, note: str | None = None) -> dict:
        view = {
            "report_id": report["report_id"],
            "status": report["status"],
            "contract": get_contract(report["contract_version"]),
            "contract_generation": report.get("contract_generation"),
            "content_digest": report["content_digest"],
            "content": report["content"],
            "approved": report["approved"],
            "allocation": report["allocation"],
            "allocation_total": report["allocation_total"],
            "unallocated": report["unallocated"],
            "receipt": report["receipt"],
            "attempts": report["attempts"],
            "last_error": report["last_error"],
            "created_at": report["created_at"],
            "updated_at": report["updated_at"],
        }
        if note:
            view["note"] = note
        return view

    def read(self, report_id: str) -> dict:
        return self._view(self.repo.get_report(report_id))

    def list(self) -> list[dict]:
        return [self._view(report) for report in self.repo.list_reports()]

    # ---- 本地接收端 ---------------------------------------------------

    def receiver_inbox(self) -> dict:
        inbox = self.receiver.inbox()
        return inbox

    def receiver_receipt(self, report_id: str) -> dict:
        receipt = self.receiver.query(report_id)
        if receipt is None:
            raise DomainError("NOT_FOUND", f"本地接收端没有报送 {report_id} 的回执", status=404)
        return receipt

    def receive(self, payload: dict) -> dict:
        """本地接收端直接收件入口，用于重放与恢复演练。

        按报送声明的契约校验：新版缺分支拒收；同一报送换内容拒收；
        同一报送同内容重复投递只返回原回执，不重复收件。
        """
        if not isinstance(payload, dict):
            raise DomainError("EMPTY_VALUE", "收件请求必须是对象")
        report_id = str(payload.get("report_id") or "").strip()
        if not report_id:
            raise DomainError("EMPTY_VALUE", "收件请求缺少报送编号")
        version = payload.get("contract_version")
        if version is None:
            raise DomainError("EMPTY_VALUE", "收件请求缺少契约版本")
        version = get_contract(version)["version"]
        content = payload.get("content")
        if content is None:
            raise DomainError("EMPTY_VALUE", "收件请求缺少原文")
        receipt = self.receiver.intake(report_id, version, content, payload.get("content_digest"))
        return {"receipt": receipt, "note": "ACCEPTED"}

    def set_receiver_available(self, available: bool) -> dict:
        return {"available": self.receiver.set_available(bool(available))}
