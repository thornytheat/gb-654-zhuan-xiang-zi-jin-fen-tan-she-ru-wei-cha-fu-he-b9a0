from fastapi import APIRouter, Body, Request

from . import service
from .repository import ReportRepository

router = APIRouter(tags=["M7-财政报送"])


def _repo(request: Request) -> ReportRepository:
    return request.app.state.reports


@router.get("/reports")
def page(request: Request):
    data = {"reports": service.execute("list", _repo(request)),
            "contract": service.execute("contract", _repo(request)),
            "status": "READY", "reason": "OK"}
    return request.app.state.templates.TemplateResponse(request, "m7_reports.html",
                                                       {"title": "财政报送", "data": data})


@router.get("/api/reports/contract")
def read_contract(request: Request):
    return service.execute("contract", _repo(request))


@router.post("/api/reports/contract/switch")
def switch_contract(request: Request, payload: dict = Body(default={})):
    return service.execute("switch_contract", _repo(request), payload)


@router.get("/api/reports")
def list_reports(request: Request):
    return {"reports": service.execute("list", _repo(request))}


@router.post("/api/reports")
def create_report(request: Request, payload: dict = Body(default={})):
    return service.execute("create", _repo(request), payload)


@router.get("/api/reports/{report_id}")
def read_report(request: Request, report_id: str):
    return service.execute("read", _repo(request), report_id)


@router.post("/api/reports/{report_id}/submit")
def submit_report(request: Request, report_id: str, payload: dict = Body(default={})):
    return service.execute("submit", _repo(request), report_id, payload)


@router.post("/api/reports/{report_id}/recover")
def recover_report(request: Request, report_id: str):
    return service.execute("recover", _repo(request), report_id)


@router.get("/api/receiver/inbox")
def receiver_inbox(request: Request):
    return service.execute("receiver_inbox", _repo(request))


@router.get("/api/receiver/inbox/{report_id}")
def receiver_receipt(request: Request, report_id: str):
    return service.execute("receiver_receipt", _repo(request), report_id)


@router.post("/api/receiver/intake")
def receiver_intake(request: Request, payload: dict = Body(default={})):
    return service.execute("receive", _repo(request), payload)


@router.post("/api/receiver/availability")
def receiver_availability(request: Request, payload: dict = Body(default={})):
    return service.execute("set_receiver_available", _repo(request), bool(payload.get("available", True)))
