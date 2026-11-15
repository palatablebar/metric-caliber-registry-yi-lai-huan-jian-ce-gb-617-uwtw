"""环处置记录：每个基本环一条台账，状态机 open -> resolved（可重开）。

三张表：
- cycle_records            主表：fingerprint 唯一建档，冗余当前内容便于查询；
- cycle_record_revisions   内容版本：口径/影响面每次变化追加一条，旧版本永不覆盖；
- cycle_record_events      处置事件流：detected/confirmed/reopened/resolved，
                           (record_id, run_id, event) 唯一，同一快照重跑不重复追加。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class CycleRecord(Base):
    __tablename__ = "cycle_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    record_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    # 同一问题识别键：环上指标集合 + 环内引用边集合的 sha256（graph_analysis.cycle_fingerprint）
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # 当前内容（最近一次校验确认的值；历史版本在 cycle_record_revisions，不覆盖）
    members: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    path: Mapped[list] = mapped_column(JSON, default=list)
    component: Mapped[list] = mapped_column(JSON, default=list)
    length: Mapped[int] = mapped_column(Integer, default=0)
    severity: Mapped[str] = mapped_column(String(12), default="low")
    score: Mapped[int] = mapped_column(Integer, default=0)
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String(300), default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    # 追踪：首次发现 / 最后确认 / 闭环结果
    first_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {broken_edges, note}
    revision_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CycleRecordRevision(Base):
    """内容版本：成员/边/影响面每次变化追加一条，任何调整都可回查，不会被覆盖。"""

    __tablename__ = "cycle_record_revisions"
    __table_args__ = (UniqueConstraint("record_id", "revision", name="uq_cycle_revision"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("cycle_records.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    members: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    path: Mapped[list] = mapped_column(JSON, default=list)
    severity: Mapped[str] = mapped_column(String(12), default="low")
    score: Mapped[int] = mapped_column(Integer, default=0)
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CycleRecordEvent(Base):
    """处置事件流：谁在何时对哪条记录做了什么；按 (record, run, event) 幂等。"""

    __tablename__ = "cycle_record_events"
    __table_args__ = (UniqueConstraint("record_id", "run_id", "event", name="uq_cycle_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("cycle_records.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    event: Mapped[str] = mapped_column(String(16))
    actor_email: Mapped[str] = mapped_column(String(160), default="system")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
