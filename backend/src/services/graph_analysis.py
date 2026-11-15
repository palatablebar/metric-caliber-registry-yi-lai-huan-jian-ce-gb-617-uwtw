"""算法 B｜依赖环检测 + 同名口径冲突 + 影响面打分。

输入都是纯 dict（与 ORM 解耦，便于单测）：
- metrics: [{code, name, domain, tier, owner, signature, aggs, fields, filters, dims, deps, parse_status}]
- reports_by_metric: {metric_code: [{code, name, is_core, weight, owner}]}

规则：
- 图 `metric -> 引用指标`；Tarjan 求 SCC，长度 > 1 或自环即为环；
- 一个 SCC 内可能有多条基本环，逐条枚举（不再合并成一条代表路径），
  每条环用「环上指标 + 环内引用边」生成 fingerprint，供处置记录识别同一问题；
- 同名指标（name 相同）但公式签名（字段/过滤/聚合/引用）不同 -> conflict；
- 影响面 = 下游报表数按权重（核心报表 ×3）求和，score >= 8 high / >= 3 medium / 否则 low。

纯确定性：节点按字典序遍历，同一输入两次输出逐字相等。
"""
from __future__ import annotations

import hashlib
import json

from src.constants.enums import REPORT_WEIGHT_CORE, REPORT_WEIGHT_NORMAL, Severity

SEVERITY_THRESHOLDS = ((8, Severity.HIGH.value), (3, Severity.MEDIUM.value))

# 单个 SCC 内枚举基本环的上限（防御病态稠密图组合爆炸；超出部分按字典序截断，结果仍确定）
MAX_CYCLES_PER_COMPONENT = 64


def severity_of(score: int) -> str:
    for threshold, label in SEVERITY_THRESHOLDS:
        if score >= threshold:
            return label
    return Severity.LOW.value


def _report_weight(report: dict) -> int:
    if report.get("weight") is not None:
        return int(report["weight"])
    return REPORT_WEIGHT_CORE if report.get("is_core") else REPORT_WEIGHT_NORMAL


def _downstream_reports(codes: list[str], reports_by_metric: dict[str, list[dict]]) -> list[dict]:
    found: dict[str, dict] = {}
    for code in codes:
        for report in reports_by_metric.get(code, []):
            found.setdefault(report["code"], report)
    return [found[key] for key in sorted(found)]


def impact_score(reports: list[dict]) -> int:
    return sum(_report_weight(report) for report in reports)


def impact_payload(reports: list[dict]) -> dict:
    score = impact_score(reports)
    return {
        "reports": [report["code"] for report in reports],
        "report_names": [report["name"] for report in reports],
        "report_count": len(reports),
        "core_reports": [report["code"] for report in reports if report.get("is_core")],
        "core_count": sum(1 for report in reports if report.get("is_core")),
        "score": score,
        "severity": severity_of(score),
    }


def build_dep_map(metrics: list[dict]) -> dict[str, list[str]]:
    known = {metric["code"] for metric in metrics}
    dep_map: dict[str, list[str]] = {}
    for metric in sorted(metrics, key=lambda item: item["code"]):
        refs = [ref for ref in (metric.get("deps") or []) if ref in known]
        dep_map[metric["code"]] = sorted(set(refs))
    return dep_map


def tarjan_scc(dep_map: dict[str, list[str]]) -> list[list[str]]:
    """迭代式 Tarjan（避免递归深度问题），输出稳定的 SCC 列表。"""
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []

    for root in sorted(dep_map):
        if root in indices:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work.pop()
            if child_index == 0:
                indices[node] = index_counter
                lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack[node] = True
            neighbours = dep_map.get(node, [])
            if child_index < len(neighbours):
                work.append((node, child_index + 1))
                child = neighbours[child_index]
                if child not in indices:
                    work.append((child, 0))
                elif on_stack.get(child):
                    lowlink[node] = min(lowlink[node], indices[child])
                continue
            if lowlink[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return sorted(result)


def _elementary_cycles(dep_map: dict[str, list[str]], component: list[str]) -> list[list[str]]:
    """枚举 SCC 内全部基本环（同一连通区域里的每条环都单独输出，不再合并）。

    Johnson 去重技巧：每个环只由环上字典序最小的节点作为起点、且只允许经过
    不小于起点的节点，因此每条基本环恰好输出一次。返回节点序列（不含回到起点
    的重复尾节点），按 (长度, 序列) 排序，确定可复现。
    """
    comp_set = set(component)
    found: list[list[str]] = []
    for start in sorted(comp_set):
        allowed = {node for node in comp_set if node >= start}
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            if len(found) >= MAX_CYCLES_PER_COMPONENT:
                break
            node, path = stack.pop()
            for neighbour in dep_map.get(node, []):
                if neighbour == start:
                    found.append(path)
                elif neighbour in allowed and neighbour not in path:
                    stack.append((neighbour, path + [neighbour]))
    found.sort(key=lambda nodes: (len(nodes), nodes))
    return found


def cycle_fingerprint(members: list[str], edges: list[tuple[str, str]]) -> str:
    """同一环问题的识别键：环上指标集合 + 环内引用边集合。

    与路径起点/方向表示无关：同一问题换了个起点输出，指纹不变；
    环上指标或引用关系变了（口径调整），指纹随之变化，按新问题建档。
    """
    payload = json.dumps(
        {
            "members": sorted(members),
            "edges": sorted(f"{src}->{dst}" for src, dst in edges),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def find_cycles(metrics: list[dict], reports_by_metric: dict[str, list[dict]]) -> list[dict]:
    dep_map = build_dep_map(metrics)
    by_code = {metric["code"]: metric for metric in metrics}
    cycles: list[dict] = []
    for component in tarjan_scc(dep_map):
        is_cycle = len(component) > 1 or any(code in dep_map.get(code, []) for code in component)
        if not is_cycle:
            continue
        component_members = sorted(component)
        for nodes in _elementary_cycles(dep_map, component):
            edges = [(nodes[index], nodes[(index + 1) % len(nodes)]) for index in range(len(nodes))]
            fingerprint = cycle_fingerprint(nodes, edges)
            path = nodes + [nodes[0]]
            reports = _downstream_reports(sorted(nodes), reports_by_metric)
            impact = impact_payload(reports)
            cycles.append(
                {
                    "id": "cycle-" + fingerprint[:12],
                    "fingerprint": fingerprint,
                    "path": path,
                    "length": len(nodes),
                    "members": sorted(nodes),
                    "edges": [{"from": src, "to": dst} for src, dst in sorted(edges)],
                    "component": component_members,
                    "metric_names": [by_code[code]["name"] for code in nodes if code in by_code],
                    "severity": impact["severity"],
                    "score": impact["score"],
                    "impact": impact,
                    "suggestion": "在环上任选一个指标改用上游聚合字段，打断引用链后再跑一次校验。",
                    "summary": f"检测到 {len(nodes)} 个指标的循环引用：{' → '.join(path)}",
                }
            )
    cycles.sort(key=lambda item: (-item["length"], item["fingerprint"]))
    return cycles


def _conflict_kind(variants: list[dict]) -> str:
    dep_sets = {tuple(sorted(item.get("deps") or [])) for item in variants}
    field_sets = {tuple(sorted(item.get("fields") or [])) for item in variants}
    filter_sets = {tuple(sorted((item.get("filters") or []))) for item in variants}
    agg_sets = {tuple(sorted(item.get("aggs") or [])) for item in variants}
    if len(dep_sets) > 1:
        return "dep_diff"
    if len(field_sets) > 1:
        return "field_diff"
    if len(filter_sets) > 1:
        return "filter_diff"
    if len(agg_sets) > 1:
        return "agg_diff"
    return "filter_diff"


def _diff_detail(variants: list[dict]) -> dict:
    fields_first = set(variants[0].get("fields") or [])
    fields_rest: set[str] = set()
    for item in variants[1:]:
        fields_rest |= set(item.get("fields") or [])
    filters_first = list(variants[0].get("filters") or [])
    filters_rest: list[str] = []
    for item in variants[1:]:
        for condition in item.get("filters") or []:
            if condition not in filters_rest:
                filters_rest.append(condition)
    return {
        "fields_only_first": sorted(fields_first - fields_rest),
        "fields_only_others": sorted(fields_rest - fields_first),
        "filters_first": filters_first,
        "filters_others": filters_rest,
        "filters_only_first": [item for item in filters_first if item not in filters_rest],
        "filters_only_others": [item for item in filters_rest if item not in filters_first],
    }


def find_conflicts(metrics: list[dict], reports_by_metric: dict[str, list[dict]]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for metric in metrics:
        if metric.get("parse_status") == "error":
            continue
        grouped.setdefault(metric["name"], []).append(metric)
    conflicts: list[dict] = []
    for name in sorted(grouped):
        variants = sorted(grouped[name], key=lambda item: item["code"])
        signatures = {item.get("signature") or "" for item in variants}
        if len(variants) < 2 or len(signatures) < 2:
            continue
        codes = [item["code"] for item in variants]
        reports = _downstream_reports(codes, reports_by_metric)
        impact = impact_payload(reports)
        kind = _conflict_kind(variants)
        severity = impact["severity"]
        conflicts.append(
            {
                "id": "conflict-" + "-".join(codes),
                "name": name,
                "kind": kind,
                "severity": severity,
                "score": impact["score"],
                "metric_codes": codes,
                "metrics": [
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "domain": item.get("domain"),
                        "tier": item.get("tier"),
                        "owner": item.get("owner"),
                        "signature": item.get("signature"),
                        "fields": item.get("fields") or [],
                        "filters": item.get("filters") or [],
                        "aggs": item.get("aggs") or [],
                        "deps": item.get("deps") or [],
                        "expression": item.get("expression") or "",
                    }
                    for item in variants
                ],
                "diff": _diff_detail(variants),
                "impact": impact,
                "suggestion": _suggestion(kind, codes, severity),
                "summary": f"「{name}」有 {len(variants)} 份不同口径："
                + "；".join(f"{item['code']}（{item.get('expression') or '—'}）" for item in variants),
            }
        )
    conflicts.sort(key=lambda item: (-item["score"], item["id"]))
    return conflicts


def _suggestion(kind: str, codes: list[str], severity: str) -> str:
    if kind == "filter_diff":
        base = f"把 {codes[0]} 与 {codes[1]} 的过滤条件对齐（含/不含退款、时间窗口），只保留一个正式口径。"
    elif kind == "field_diff":
        base = f"{codes[0]} 与 {codes[1]} 用了不同字段，确认数仓字段口径后统一到其中一个。"
    elif kind == "dep_diff":
        base = f"{codes[0]} 与 {codes[1]} 引用了不同指标，理清推导关系并补一条变更单。"
    else:
        base = f"{codes[0]} 与 {codes[1]} 聚合方式不同，确认月会口径后统一。"
    if severity == Severity.HIGH.value:
        return base + " 影响核心报表，需在月会前走变更单批准。"
    return base


def impact_of_metric(code: str, metrics: list[dict], reports_by_metric: dict[str, list[dict]]) -> dict:
    reports = _downstream_reports([code], reports_by_metric)
    impact = impact_payload(reports)
    impact.update({"kind": "metric", "target": code, "metric_codes": [code], "metric_count": 1})
    return impact


def impact_of_field(field: str, metrics: list[dict], reports_by_metric: dict[str, list[dict]]) -> dict:
    affected = sorted(
        metric["code"]
        for metric in metrics
        if field in (metric.get("fields") or [])
    )
    reports = _downstream_reports(affected, reports_by_metric)
    impact = impact_payload(reports)
    impact.update(
        {
            "kind": "field",
            "target": field,
            "metric_codes": affected,
            "metric_count": len(affected),
        }
    )
    return impact


def summarize(metrics: list[dict], reports_by_metric: dict[str, list[dict]]) -> dict:
    conflicts = find_conflicts(metrics, reports_by_metric)
    cycles = find_cycles(metrics, reports_by_metric)
    parse_errors = [metric["code"] for metric in metrics if metric.get("parse_status") == "error"]
    severity_counter = {Severity.HIGH.value: 0, Severity.MEDIUM.value: 0, Severity.LOW.value: 0}
    for item in conflicts + cycles:
        severity_counter[item["severity"]] = severity_counter.get(item["severity"], 0) + 1
    return {
        "metrics_total": len(metrics),
        "conflicts_total": len(conflicts),
        "cycles_total": len(cycles),
        "parse_errors": parse_errors,
        "parse_error_count": len(parse_errors),
        "severity": severity_counter,
        "conflicts": conflicts,
        "cycles": cycles,
        "edge_count": sum(len(refs) for refs in build_dep_map(metrics).values()),
    }
