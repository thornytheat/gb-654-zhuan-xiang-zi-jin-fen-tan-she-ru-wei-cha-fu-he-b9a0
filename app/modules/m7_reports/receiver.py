"""本地接收端：模拟财政收件，不连接真实财政系统。

接收端独立按报送契约校验原文，并按报送编号幂等收件：
同一报送同一内容只产生一张回执，换内容拒收。
"""
from __future__ import annotations

from app.errors import DomainError
from app.manifest import canonical_digest

from .contracts import validate_content
from .repository import ReportRepository


class LocalReceiver:
    def __init__(self, repository: ReportRepository):
        self.repo = repository

    @property
    def available(self) -> bool:
        return self.repo.receiver_available()

    def set_available(self, available: bool) -> bool:
        return self.repo.set_receiver_available(available)

    def intake(self, report_id: str, contract_version: int, content: dict,
               content_digest: str | None = None) -> dict:
        """收件。先按报送声明的契约校验，再幂等落库。"""
        if not self.available:
            raise DomainError("RECEIVER_UNAVAILABLE", "本地接收端暂不可用", status=503)
        validate_content(content, contract_version)
        digest = canonical_digest(
            {"contract_version": int(contract_version), "content": content}
        )
        if content_digest is not None and content_digest != digest:
            raise DomainError(
                "CONTENT_CONFLICT",
                "收件摘要与原文不一致，拒绝收件",
                status=409,
            )
        return self.repo.receiver_accept(
            report_id,
            int(contract_version),
            digest,
            receipt_no="RCP-" + digest[:12],
        )

    def query(self, report_id: str) -> dict | None:
        return self.repo.receiver_receipt(report_id)

    def inbox(self) -> dict:
        receipts = self.repo.receiver_inbox()
        return {
            "available": self.available,
            "accepted_count": len(receipts),
            "receipts": receipts,
        }
