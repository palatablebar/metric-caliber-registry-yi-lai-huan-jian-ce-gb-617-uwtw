"""依赖图控制器：边列表 + 图结构 + 环处置记录。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import cycle_record_service, dependency_service, graph_service


def list_edges(db: Session, **filters) -> dict:
    return dependency_service.list_edges(db, **filters)


def graph(db: Session) -> dict:
    return graph_service.graph(db)


def cycles(db: Session, *, status: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    return cycle_record_service.list_records(db, status=status, limit=limit, offset=offset)


def cycle_detail(db: Session, record_no: str) -> dict:
    return cycle_record_service.record_detail(db, record_no)


def impact(db: Session, *, field: str | None = None, metric: str | None = None) -> dict:
    if field:
        return graph_service.impact_for_field(db, field)
    if metric:
        return graph_service.impact_for_metric(db, metric)
    return {"kind": "none", "score": 0, "severity": "low", "reports": [], "report_count": 0}
