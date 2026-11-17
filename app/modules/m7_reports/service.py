from . import core


def service(repository) -> core.ReportService:
    return core.ReportService(repository)


def execute(method: str, repository, *args, **kwargs):
    return getattr(service(repository), method)(*args, **kwargs)
