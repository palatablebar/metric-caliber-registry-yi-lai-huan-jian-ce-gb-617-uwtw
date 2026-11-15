"""环处置记录（cycle_record_service）单测：建档 / 幂等 / 闭环留痕 / 版本不覆盖 / 重开。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.models import Base
from src.models.cycle_record import CycleRecord, CycleRecordEvent, CycleRecordRevision
from src.models.run import Run
from src.services import cycle_record_service, graph_analysis


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with factory() as session:
        yield session


def _metric(code, deps=None, name=None):
    return {
        "code": code,
        "name": name or code.upper(),
        "domain": "trade",
        "tier": "normal",
        "owner": "测试",
        "signature": f"sig-{code}",
        "parse_status": "ok",
        "aggs": ["sum"],
        "fields": ["t.x"],
        "filters": [],
        "dims": [],
        "deps": deps or [],
        "expression": f"{code} = sum(t.x)",
    }


def _run(db, run_no, operator="analyst@metricreg.io"):
    run = Run(run_no=run_no, trigger="manual", status="running", operator_email=operator)
    db.add(run)
    db.flush()
    return run


def _cycles(metrics, reports=None):
    return graph_analysis.find_cycles(metrics, reports or {})


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_one_component_yields_every_elementary_cycle():
    """同一连通区域里的每条基本环都单独输出，不再合并成一条代表路径。"""
    metrics = [_metric("a", ["b"]), _metric("b", ["a", "c"]), _metric("c", ["b"])]
    cycles = _cycles(metrics)
    assert len(cycles) == 2
    assert sorted(tuple(cycle["members"]) for cycle in cycles) == [("a", "b"), ("b", "c")]
    assert len({cycle["fingerprint"] for cycle in cycles}) == 2
    for cycle in cycles:
        assert cycle["component"] == ["a", "b", "c"]
        assert cycle["edges"]


def test_fingerprint_identifies_same_problem_regardless_of_rotation():
    fp_a = graph_analysis.cycle_fingerprint(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    fp_b = graph_analysis.cycle_fingerprint(["c", "a", "b"], [("c", "a"), ("a", "b"), ("b", "c")])
    assert fp_a == fp_b
    fp_other = graph_analysis.cycle_fingerprint(["a", "b"], [("a", "b"), ("b", "a")])
    assert fp_other != fp_a


def test_reconcile_same_snapshot_is_idempotent(db):
    """重新运行同一份关系快照：不新增记录、不追加版本、不重复事件。"""
    metrics = [_metric("a", ["b"]), _metric("b", ["a"])]
    cycles = _cycles(metrics)
    dep_map = graph_analysis.build_dep_map(metrics)
    run = _run(db, "V20261115-0001")

    first = cycle_record_service.reconcile(db, run=run, cycles=cycles, dep_map=dep_map, operator_email="op@x.io")
    assert first["new"] == 1 and first["resolved"] == 0
    cycle_record_service.reconcile(db, run=run, cycles=cycles, dep_map=dep_map, operator_email="op@x.io")
    cycle_record_service.reconcile(db, run=run, cycles=cycles, dep_map=dep_map, operator_email="op@x.io")

    assert _count(db, CycleRecord) == 1
    assert _count(db, CycleRecordRevision) == 1
    # 同一运行：detected + confirmed 各一条，不重复追加
    assert _count(db, CycleRecordEvent) == 2

    run2 = _run(db, "V20261115-0002")
    second = cycle_record_service.reconcile(db, run=run2, cycles=cycles, dep_map=dep_map, operator_email="op@x.io")
    assert second["new"] == 0 and second["confirmed"] == 1 and second["revisions"] == 0
    assert _count(db, CycleRecord) == 1
    assert _count(db, CycleRecordRevision) == 1
    record = db.scalar(select(CycleRecord))
    assert record.first_seen_run_id == run.id
    assert record.last_seen_run_id == run2.id
    assert record.status == "open"


def test_resolve_keeps_members_edges_impact_and_broken_edge(db):
    """公式修好后：记录闭环但成员/边/当时的下游影响保留，且记下谁打断了哪条边。"""
    metrics = [_metric("a", ["b"]), _metric("b", ["a"])]
    reports = {"a": [{"code": "r01", "name": "经营日报", "is_core": True, "weight": 3}]}
    cycles = _cycles(metrics, reports)
    run1 = _run(db, "V20261115-0001")
    cycle_record_service.reconcile(
        db, run=run1, cycles=cycles, dep_map=graph_analysis.build_dep_map(metrics), operator_email="op@x.io"
    )

    fixed = [_metric("a"), _metric("b", ["a"])]  # a 不再引用 b：边 a->b 被打断
    run2 = _run(db, "V20261115-0002", operator="admin@metricreg.io")
    stats = cycle_record_service.reconcile(
        db, run=run2, cycles=_cycles(fixed), dep_map=graph_analysis.build_dep_map(fixed), operator_email="admin@metricreg.io"
    )
    assert stats["resolved"] == 1
    assert stats["open_total"] == 0  # 图上已无环 -> 不存在未闭环记录

    record = db.scalar(select(CycleRecord))
    assert record.status == "resolved"
    assert record.resolved_run_id == run2.id
    assert record.resolved_by == "admin@metricreg.io"
    assert record.resolved_at is not None
    # 闭环结果：被打断的边
    assert record.resolution["broken_edges"] == [{"from": "a", "to": "b"}]
    # 修好后的环仍保留成员、边和当时的下游影响
    assert record.members == ["a", "b"]
    assert {tuple(sorted(edge.items())) for edge in record.edges} == {
        tuple(sorted(edge.items())) for edge in [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]
    }
    assert record.impact["reports"] == ["r01"]
    assert record.score == 3
    # 事件流里能回答「谁在何时打断了哪条边」
    events = db.scalars(select(CycleRecordEvent).order_by(CycleRecordEvent.id)).all()
    resolved_events = [event for event in events if event.event == "resolved"]
    assert len(resolved_events) == 1
    assert resolved_events[0].actor_email == "admin@metricreg.io"
    assert resolved_events[0].run_id == run2.id
    assert resolved_events[0].detail["broken_edges"] == [{"from": "a", "to": "b"}]


def test_caliber_change_appends_revision_never_overwrites(db):
    """口径调整（影响面变化）：追加内容版本，旧版本可回查，不被覆盖。"""
    metrics = [_metric("a", ["b"]), _metric("b", ["a"])]
    dep_map = graph_analysis.build_dep_map(metrics)
    run1 = _run(db, "V20261115-0001")
    cycle_record_service.reconcile(
        db, run=run1, cycles=_cycles(metrics), dep_map=dep_map, operator_email="op@x.io"
    )
    reports = {"a": [{"code": "r01", "name": "经营日报", "is_core": True, "weight": 3}]}
    run2 = _run(db, "V20261115-0002")
    stats = cycle_record_service.reconcile(
        db, run=run2, cycles=_cycles(metrics, reports), dep_map=dep_map, operator_email="op@x.io"
    )
    assert stats["revisions"] == 1

    record = db.scalar(select(CycleRecord))
    assert record.revision_count == 2
    assert record.score == 3  # 主表是最新值
    revisions = db.scalars(select(CycleRecordRevision).order_by(CycleRecordRevision.revision)).all()
    assert len(revisions) == 2
    assert revisions[0].score == 0 and revisions[0].impact["reports"] == []  # 旧版本仍在
    assert revisions[1].score == 3 and revisions[1].impact["reports"] == ["r01"]


def test_reopen_when_same_cycle_comes_back(db):
    """闭环后同一 fingerprint 再现 -> 重开，历史闭环结果留在事件流里。"""
    metrics = [_metric("a", ["b"]), _metric("b", ["a"])]
    dep_map = graph_analysis.build_dep_map(metrics)
    fixed = [_metric("a"), _metric("b", ["a"])]
    run1 = _run(db, "V20261115-0001")
    cycle_record_service.reconcile(db, run=run1, cycles=_cycles(metrics), dep_map=dep_map, operator_email="op@x.io")
    run2 = _run(db, "V20261115-0002")
    cycle_record_service.reconcile(
        db, run=run2, cycles=[], dep_map=graph_analysis.build_dep_map(fixed), operator_email="op@x.io"
    )
    run3 = _run(db, "V20261115-0003")
    stats = cycle_record_service.reconcile(db, run=run3, cycles=_cycles(metrics), dep_map=dep_map, operator_email="op@x.io")
    assert stats["reopened"] == 1 and stats["new"] == 0

    assert _count(db, CycleRecord) == 1
    record = db.scalar(select(CycleRecord))
    assert record.status == "open"
    assert record.resolved_at is None
    kinds = [event.event for event in db.scalars(select(CycleRecordEvent).order_by(CycleRecordEvent.id)).all()]
    assert kinds == ["detected", "resolved", "reopened", "confirmed"]


def test_read_paths_share_one_state(db):
    """总览 / 依赖图 / 导出读同一批状态：open_cycles、list_records、stats 一致。"""
    metrics = [_metric("a", ["b"]), _metric("b", ["a", "c"]), _metric("c", ["b"])]
    run1 = _run(db, "V20261115-0001")
    cycle_record_service.reconcile(
        db, run=run1, cycles=_cycles(metrics), dep_map=graph_analysis.build_dep_map(metrics), operator_email="op@x.io"
    )
    open_items = cycle_record_service.open_cycles(db)
    assert len(open_items) == 2  # 同一区域两条基本环，各自一条记录
    assert cycle_record_service.stats(db) == {"open": 2, "resolved": 0, "total": 2}
    ledger = cycle_record_service.list_records(db)
    assert ledger["total"] == 2
    assert {item["record_no"] for item in ledger["items"]} == {item["record_no"] for item in open_items}
    detail = cycle_record_service.record_detail(db, open_items[0]["record_no"])
    assert detail["events"] and detail["revisions"]

    # 只打断 a↔b：同一区域里另一条环 b↔c 的记录必须保持未闭环
    fixed = [_metric("a"), _metric("b", ["a", "c"]), _metric("c", ["b"])]
    run2 = _run(db, "V20261115-0002")
    stats = cycle_record_service.reconcile(
        db, run=run2, cycles=_cycles(fixed), dep_map=graph_analysis.build_dep_map(fixed), operator_email="op@x.io"
    )
    assert stats["resolved"] == 1
    remaining = cycle_record_service.open_cycles(db)
    assert len(remaining) == 1
    assert remaining[0]["members"] == ["b", "c"]
    assert cycle_record_service.stats(db) == {"open": 1, "resolved": 1, "total": 2}

    # 全部打断：图上无环 -> 不存在未闭环记录
    dag = [_metric("a"), _metric("b", ["a"]), _metric("c", ["b"])]
    run3 = _run(db, "V20261115-0003")
    cycle_record_service.reconcile(
        db, run=run3, cycles=_cycles(dag), dep_map=graph_analysis.build_dep_map(dag), operator_email="op@x.io"
    )
    assert cycle_record_service.open_cycles(db) == []
    assert cycle_record_service.stats(db)["resolved"] == 2
    assert cycle_record_service.list_records(db, status="resolved")["total"] == 2
