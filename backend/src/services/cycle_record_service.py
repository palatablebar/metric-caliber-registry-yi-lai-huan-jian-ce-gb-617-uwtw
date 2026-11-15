"""环处置记录服务：校验运行时 reconcile（唯一写入口），总览/依赖图/导出统一读这里。

状态机：
- 校验检出 fingerprint 未知的环 -> 建档 open（记首次发现）；
- 已建档的环再次被检出 -> 确认（刷新最后确认；内容有变化只追加版本，不覆盖）；
- 已闭环的环同一 fingerprint 再现 -> 重开（历史闭环结果保留在事件流里）；
- open 记录本次未被检出 -> 闭环（记闭环运行/操作人/被打断的引用边）。

幂等：fingerprint 唯一建档；事件按 (record, run, event) 去重；内容不变不追加版本。
重新跑同一份关系快照不会产生新记录、新版本或重复事件。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import CycleEvent, CycleStatus
from src.models.cycle_record import CycleRecord, CycleRecordEvent, CycleRecordRevision
from src.models.metric import Metric
from src.models.run import Run
from src.utils.errors import not_found
from src.utils.timeutil import iso_utc, utcnow


def _apply_snapshot(record: CycleRecord, cycle: dict) -> None:
    record.members = cycle["members"]
    record.edges = cycle["edges"]
    record.path = cycle["path"]
    record.component = cycle.get("component") or cycle["members"]
    record.length = cycle["length"]
    record.severity = cycle["severity"]
    record.score = cycle["score"]
    record.impact = cycle["impact"]
    record.summary = cycle["summary"]
    record.suggestion = cycle["suggestion"]


def _content_changed(record: CycleRecord, cycle: dict) -> bool:
    """fingerprint 相同则成员/边必然相同；这里识别的是影响面/严重度等口径变化。"""
    return (
        record.members != cycle["members"]
        or record.edges != cycle["edges"]
        or record.impact != cycle["impact"]
        or record.severity != cycle["severity"]
        or record.score != cycle["score"]
    )


def _next_record_no(db: Session) -> str:
    suffixes = [
        int(no.split("-", 1)[1])
        for no in db.scalars(select(CycleRecord.record_no)).all()
        if no.startswith("CYC-") and no.split("-", 1)[1].isdigit()
    ]
    return f"CYC-{(max(suffixes) + 1) if suffixes else 1:04d}"


def _add_revision(db: Session, record: CycleRecord, run: Run, cycle: dict, note: str) -> None:
    db.add(
        CycleRecordRevision(
            record_id=record.id,
            revision=record.revision_count,
            run_id=run.id,
            members=cycle["members"],
            edges=cycle["edges"],
            path=cycle["path"],
            severity=cycle["severity"],
            score=cycle["score"],
            impact=cycle["impact"],
            note=note,
        )
    )


def _add_event(
    db: Session,
    record: CycleRecord,
    run: Run,
    event: str,
    actor_email: str,
    detail: dict | None = None,
) -> None:
    """幂等：同一运行对同一记录的同种事件只记一次。"""
    exists = db.scalar(
        select(CycleRecordEvent.id).where(
            CycleRecordEvent.record_id == record.id,
            CycleRecordEvent.run_id == run.id,
            CycleRecordEvent.event == event,
        )
    )
    if exists is not None:
        return
    db.add(
        CycleRecordEvent(
            record_id=record.id,
            run_id=run.id,
            event=event,
            actor_email=actor_email,
            detail=detail or {},
        )
    )


def reconcile(
    db: Session,
    *,
    run: Run,
    cycles: list[dict],
    dep_map: dict[str, list[str]],
    operator_email: str,
) -> dict:
    """把本次校验检出的基本环与台账对齐（每次校验运行调用一次）。"""
    now = utcnow()
    records = {record.fingerprint: record for record in db.scalars(select(CycleRecord)).all()}
    current_edges = {(code, ref) for code, refs in dep_map.items() for ref in refs}
    stats = {"new": 0, "confirmed": 0, "reopened": 0, "resolved": 0, "revisions": 0}
    seen: set[str] = set()
    for cycle in cycles:
        fingerprint = cycle["fingerprint"]
        seen.add(fingerprint)
        record = records.get(fingerprint)
        if record is None:
            record = CycleRecord(
                record_no=_next_record_no(db),
                fingerprint=fingerprint,
                status=CycleStatus.OPEN.value,
                first_seen_run_id=run.id,
                first_seen_at=now,
                revision_count=1,
            )
            _apply_snapshot(record, cycle)
            db.add(record)
            db.flush()
            _add_revision(db, record, run, cycle, note="首次检出")
            _add_event(db, record, run, CycleEvent.DETECTED.value, operator_email)
            records[fingerprint] = record
            stats["new"] += 1
        else:
            if record.status == CycleStatus.RESOLVED.value:
                record.status = CycleStatus.OPEN.value
                record.resolved_run_id = None
                record.resolved_at = None
                record.resolved_by = None
                record.resolution = None
                _add_event(db, record, run, CycleEvent.REOPENED.value, operator_email)
                stats["reopened"] += 1
            if _content_changed(record, cycle):
                record.revision_count += 1
                _add_revision(db, record, run, cycle, note="口径/影响面调整")
                stats["revisions"] += 1
            _apply_snapshot(record, cycle)
            _add_event(db, record, run, CycleEvent.CONFIRMED.value, operator_email)
            stats["confirmed"] += 1
        record.last_seen_run_id = run.id
        record.last_seen_at = now
    for fingerprint, record in records.items():
        if fingerprint in seen or record.status != CycleStatus.OPEN.value:
            continue
        broken = [
            edge
            for edge in (record.edges or [])
            if (edge.get("from"), edge.get("to")) not in current_edges
        ]
        record.status = CycleStatus.RESOLVED.value
        record.resolved_run_id = run.id
        record.resolved_at = now
        record.resolved_by = operator_email
        record.resolution = {
            "broken_edges": broken,
            "note": "；".join(f"{edge.get('from')} → {edge.get('to')} 已断开" for edge in broken)
            or "环上引用边已不存在",
        }
        _add_event(db, record, run, CycleEvent.RESOLVED.value, operator_email, {"broken_edges": broken})
        stats["resolved"] += 1
    db.flush()
    counts = dict(
        db.execute(select(CycleRecord.status, func.count()).group_by(CycleRecord.status)).all()
    )
    stats["open_total"] = counts.get(CycleStatus.OPEN.value, 0)
    stats["resolved_total"] = counts.get(CycleStatus.RESOLVED.value, 0)
    return stats


def _run_nos(db: Session) -> dict[int, str]:
    return {run.id: run.run_no for run in db.scalars(select(Run)).all()}


def _metric_names(db: Session) -> dict[str, str]:
    return {metric.code: metric.name for metric in db.scalars(select(Metric)).all()}


def serialize(
    record: CycleRecord,
    run_nos: dict[int, str] | None = None,
    metric_names: dict[str, str] | None = None,
) -> dict:
    run_nos = run_nos or {}
    metric_names = metric_names or {}
    members = record.members or []
    return {
        "record_no": record.record_no,
        "id": "cycle-" + record.fingerprint[:12],
        "fingerprint": record.fingerprint,
        "status": record.status,
        "path": record.path or [],
        "length": record.length,
        "members": members,
        "edges": record.edges or [],
        "component": record.component or [],
        "metric_names": [metric_names.get(code, code) for code in (record.path or [])[:-1]],
        "severity": record.severity,
        "score": record.score,
        "impact": record.impact or {},
        "summary": record.summary,
        "suggestion": record.suggestion,
        "first_seen_at": iso_utc(record.first_seen_at),
        "first_seen_run": run_nos.get(record.first_seen_run_id),
        "last_seen_at": iso_utc(record.last_seen_at),
        "last_seen_run": run_nos.get(record.last_seen_run_id),
        "resolved_at": iso_utc(record.resolved_at),
        "resolved_run": run_nos.get(record.resolved_run_id),
        "resolved_by": record.resolved_by,
        "resolution": record.resolution,
        "revision_count": record.revision_count,
    }


def open_cycles(db: Session) -> list[dict]:
    """未闭环记录（总览 / 依赖图 / 导出共用这一批状态）。"""
    rows = db.scalars(
        select(CycleRecord).where(CycleRecord.status == CycleStatus.OPEN.value).order_by(CycleRecord.id)
    ).all()
    run_nos = _run_nos(db)
    names = _metric_names(db)
    items = [serialize(row, run_nos, names) for row in rows]
    items.sort(key=lambda item: (-item["length"], item["fingerprint"]))
    return items


def list_records(db: Session, *, status: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    stmt = select(CycleRecord)
    count_stmt = select(func.count()).select_from(CycleRecord)
    if status:
        stmt = stmt.where(CycleRecord.status == status)
        count_stmt = count_stmt.where(CycleRecord.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(CycleRecord.status, CycleRecord.id.desc()).limit(limit).offset(offset)
    ).all()
    run_nos = _run_nos(db)
    names = _metric_names(db)
    return {
        "items": [serialize(row, run_nos, names) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def record_detail(db: Session, record_no: str) -> dict:
    record = db.scalar(select(CycleRecord).where(CycleRecord.record_no == record_no))
    if record is None:
        raise not_found("cycle_record", record_no)
    run_nos = _run_nos(db)
    payload = serialize(record, run_nos, _metric_names(db))
    revisions = db.scalars(
        select(CycleRecordRevision)
        .where(CycleRecordRevision.record_id == record.id)
        .order_by(CycleRecordRevision.revision)
    ).all()
    events = db.scalars(
        select(CycleRecordEvent).where(CycleRecordEvent.record_id == record.id).order_by(CycleRecordEvent.id)
    ).all()
    payload["revisions"] = [
        {
            "revision": revision.revision,
            "run": run_nos.get(revision.run_id),
            "members": revision.members or [],
            "edges": revision.edges or [],
            "path": revision.path or [],
            "severity": revision.severity,
            "score": revision.score,
            "impact": revision.impact or {},
            "note": revision.note,
            "created_at": iso_utc(revision.created_at),
        }
        for revision in revisions
    ]
    payload["events"] = [
        {
            "event": event.event,
            "run": run_nos.get(event.run_id),
            "actor_email": event.actor_email,
            "detail": event.detail or {},
            "created_at": iso_utc(event.created_at),
        }
        for event in events
    ]
    return payload


def stats(db: Session) -> dict:
    counts = dict(
        db.execute(select(CycleRecord.status, func.count()).group_by(CycleRecord.status)).all()
    )
    open_count = counts.get(CycleStatus.OPEN.value, 0)
    resolved_count = counts.get(CycleStatus.RESOLVED.value, 0)
    return {"open": open_count, "resolved": resolved_count, "total": open_count + resolved_count}
