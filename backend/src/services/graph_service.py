"""依赖图服务：把数据库里的指标/公式/报表装配成算法 B 的纯 dict 输入。

环的读取统一走 cycle_record_service（校验运行 reconcile 后的台账），
总览 / 依赖图 / 导出看到的是同一批处置状态。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import METRIC_DOMAIN_LABELS, Severity
from src.models.metric import Metric
from src.services import cycle_record_service, formula_service, graph_analysis, report_service
from src.utils.errors import AppError


def collect_inputs(db: Session) -> tuple[list[dict], dict[str, list[dict]], dict[str, Metric]]:
    metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    latest = formula_service.latest_map(db)
    reports_index = report_service.reports_by_metric(db)
    payloads: list[dict] = []
    for metric in metrics:
        formula = latest.get(metric.id)
        parsed = (formula.parsed or {}) if formula else {}
        parse_status = formula.parse_status if formula else "error"
        payloads.append(
            {
                "metric_id": metric.id,
                "code": metric.code,
                "name": metric.name,
                "domain": metric.domain,
                "domain_label": METRIC_DOMAIN_LABELS.get(metric.domain, metric.domain),
                "tier": metric.tier,
                "owner": metric.owner,
                "status": metric.status,
                "version": metric.current_version,
                "signature": formula.signature if formula else "",
                "parse_status": parse_status,
                "aggs": parsed.get("aggs") or [],
                "fields": parsed.get("fields") or [],
                "filters": parsed.get("filters") or [],
                "dims": parsed.get("dims") or [],
                "deps": parsed.get("deps") or [],
                "expression": parsed.get("expression") or "",
            }
        )
    return payloads, reports_index, {metric.code: metric for metric in metrics}


def find_conflicts(db: Session) -> list[dict]:
    metrics, reports_index, _ = collect_inputs(db)
    return graph_analysis.find_conflicts(metrics, reports_index)


def find_cycles(db: Session) -> list[dict]:
    """未闭环的环处置记录（与总览 / 依赖图 / 导出同一批状态）。"""
    return cycle_record_service.open_cycles(db)


def summary(db: Session) -> dict:
    metrics, reports_index, _ = collect_inputs(db)
    conflicts = graph_analysis.find_conflicts(metrics, reports_index)
    cycles = cycle_record_service.open_cycles(db)
    parse_errors = [metric["code"] for metric in metrics if metric.get("parse_status") == "error"]
    severity_counter = {Severity.HIGH.value: 0, Severity.MEDIUM.value: 0, Severity.LOW.value: 0}
    for item in conflicts + cycles:
        severity_counter[item["severity"]] = severity_counter.get(item["severity"], 0) + 1
    record_stats = cycle_record_service.stats(db)
    return {
        "metrics_total": len(metrics),
        "conflicts_total": len(conflicts),
        "cycles_total": len(cycles),
        "cycles_open": record_stats["open"],
        "cycles_resolved": record_stats["resolved"],
        "parse_errors": parse_errors,
        "parse_error_count": len(parse_errors),
        "severity": severity_counter,
        "conflicts": conflicts,
        "cycles": cycles,
        "edge_count": sum(len(refs) for refs in graph_analysis.build_dep_map(metrics).values()),
    }


def graph(db: Session) -> dict:
    metrics, reports_index, _ = collect_inputs(db)
    dep_map = graph_analysis.build_dep_map(metrics)
    cycles = cycle_record_service.open_cycles(db)
    in_cycle = {code for cycle in cycles for code in cycle["members"]}
    # 当前实时图与台账是否一致：改了口径还没跑校验时提示，状态仍以台账为准
    live_fingerprints = {cycle["fingerprint"] for cycle in graph_analysis.find_cycles(metrics, reports_index)}
    recorded_fingerprints = {cycle["fingerprint"] for cycle in cycles}
    stale = live_fingerprints != recorded_fingerprints
    report_codes: dict[str, list[str]] = {
        code: [report["code"] for report in reports] for code, reports in reports_index.items()
    }
    nodes = [
        {
            "code": metric["code"],
            "name": metric["name"],
            "domain": metric["domain"],
            "domain_label": metric["domain_label"],
            "tier": metric["tier"],
            "version": metric["version"],
            "parse_status": metric["parse_status"],
            "in_cycle": metric["code"] in in_cycle,
            "dep_count": len(dep_map.get(metric["code"], [])),
            "report_count": len(report_codes.get(metric["code"], [])),
            "reports": report_codes.get(metric["code"], []),
        }
        for metric in metrics
    ]
    edges = [
        {"from": code, "to": ref, "kind": "metric"}
        for code in sorted(dep_map)
        for ref in dep_map[code]
    ]
    edges += [
        {"from": code, "to": report, "kind": "report"}
        for code in sorted(report_codes)
        for report in report_codes[code]
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "dep_edges": [edge for edge in edges if edge["kind"] == "metric"],
        "cycles": cycles,
        "stale": stale,
        "stats": {
            "nodes": len(nodes),
            "dep_edges": sum(len(refs) for refs in dep_map.values()),
            "report_edges": sum(len(refs) for refs in report_codes.values()),
            "cycles": len(cycles),
            "cycle_nodes": len(in_cycle),
        },
    }


def impact_for_field(db: Session, field: str) -> dict:
    metrics, reports_index, _ = collect_inputs(db)
    impact = graph_analysis.impact_of_field(field, metrics, reports_index)
    if not impact.get("metric_codes"):
        raise AppError("NOT_FOUND", 404, {"entity": "field", "field": field})
    return impact


def impact_for_metric(db: Session, code: str) -> dict:
    metrics, reports_index, by_code = collect_inputs(db)
    if code not in by_code:
        raise AppError("NOT_FOUND", 404, {"entity": "metric", "code": code})
    return graph_analysis.impact_of_metric(code, metrics, reports_index)
