"""分析实验接口：样本口径查询、分组摘要翻页、版本持久化与导出。

列表查询与翻页始终复用同一个 ``snapshot``；筛选条件、分组摘要和样本口径在
同一轮查询里固定，翻页不会看到不同样本范围。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    ConflictResolvePayload,
    EntryPayload,
    ExperimentQueryPayload,
    ExperimentSummaryPayload,
)
from app.services.experiment import experiment_service

router = APIRouter(prefix="/api/experiment", tags=["分析实验"])
service = experiment_service
LIST_FIELDS = ["方案编号", "实验名称", "分析数据集", "样本范围", "分组字段", "目标度量", "基准区间", "显著阈值"]
STATUSES = ["草稿", "已设计", "运行中", "已完成"]
EXPORT_FIELDS = [
    "方案编号", "实验名称", "样本区间", "分组字段", "分组值", "样本量",
    "样本占比", "指标均值", "对比基准", "差异", "指标总和",
]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def request_filters(request: Request) -> dict[str, str]:
    """收集样本区间、分组字段等筛选条件，忽略分页与排序保留参数。"""
    reserved = {"page", "size", "sort", "order", "snapshot", "version", "keyword", "status", "format"}
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key not in reserved and value.strip()
    }


def query_arguments(
    filters: dict[str, Any] | None,
    page: int,
    size: int,
    sort: str | None,
    order: str | None,
) -> dict[str, Any]:
    return {
        "filters": filters or {},
        "page": page,
        "size": size,
        "sort": sort,
        "order": order,
    }


def merged_values(payload: ExperimentQueryPayload) -> dict[str, Any]:
    """筛选条件可直接放 values、filters，也可平铺在请求体；三者合并，values 优先。"""
    merged: dict[str, Any] = dict(payload.model_extra or {})
    merged.update(payload.filters or {})
    merged.update(payload.values or {})
    return merged


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    return {"metrics": service.kpi_metrics()}


@router.get("/metadata")
def get_metadata() -> dict[str, Any]:
    """样本区间、分组字段与可选维度，供页面初始化筛选器。"""
    return service.metadata()


@router.get("/samples")
def list_samples(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=500),
) -> dict[str, Any]:
    """只读实验样本明细，用于核对摘要与样本口径来自同一段来源。"""
    return service.list_samples(request_filters(request), page=page, size=size)


@router.get("/versions")
def list_versions() -> dict[str, Any]:
    return service.list_versions()


@router.get("/conflicts")
def list_conflicts() -> dict[str, Any]:
    return service.list_conflicts()


@router.get("/export")
def export_entries(
    request: Request,
    snapshot: str | None = Query(default=None),
    version: int | None = Query(default=None),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
) -> StreamingResponse:
    """导出与页面同一口径的全量分组摘要；必须声明在 /{entry_id} 之前。"""
    items, snapshot_id = service.export_rows(
        request_filters(request), snapshot=snapshot, version=version, sort=sort, order=order)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *EXPORT_FIELDS, "口径编号"])
    for item in items:
        row = [str(item.get("id", ""))]
        row.extend(csv_cell(item.get(field, "")) for field in EXPORT_FIELDS)
        row.append(csv_cell(snapshot_id))
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="experiment.csv"'})


@router.get("")
def list_results(
    request: Request,
    snapshot: str | None = Query(default=None, description="首次查询返回的样本口径编号"),
    version: int | None = Query(default=None, description="按摘要版本读取同一份口径"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=500),
    sort: str | None = Query(default=None, description="样本量、指标均值、差异等"),
    order: str | None = Query(default=None, description="asc 或 desc"),
) -> dict[str, Any]:
    """按筛选条件预聚合实验分组摘要；带 snapshot 翻页复用同一份样本。"""
    return service.query_summary(
        request_filters(request),
        snapshot=snapshot,
        version=version,
        page=page,
        size=size,
        sort=sort,
        order=order,
    )


@router.post("/query")
def query_summary(payload: ExperimentQueryPayload) -> dict[str, Any]:
    """查询模板接口：筛选条件、分组摘要与样本口径在同一轮查询里固定。"""
    return service.query_summary(
        merged_values(payload),
        snapshot=payload.snapshot,
        version=payload.version,
        page=payload.page,
        size=payload.size,
        sort=payload.sort,
        order=payload.order,
    )


@router.post("/summary")
def persist_summary(payload: ExperimentSummaryPayload) -> dict[str, Any]:
    """实验结果接口：消费并持久化实验样本摘要，核对主版本边界。"""
    return service.persist_summary(
        payload.values,
        base_version=payload.base_version,
        snapshot=payload.snapshot,
        remark=payload.remark,
    )


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    return service.run_analysis(payload.values)


@router.get("/versions/{version_id}")
def get_version(version_id: int) -> dict:
    version = service.get_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"摘要版本 {version_id} 不存在")
    return version


@router.post("/versions/{version_id}/activate")
def activate_version(version_id: int) -> dict[str, Any]:
    """回退到指定摘要版本；并发处理最终只保留一个有效主版本。"""
    version, message = service.activate_version(version_id)
    if version is None:
        return {"ok": False, "message": message, "version": None}
    return {"ok": True, "message": message, "version": version}


@router.post("/conflicts/{version_id}/resolve")
def resolve_conflict(version_id: int, payload: ConflictResolvePayload) -> dict[str, Any]:
    version, message = service.resolve_conflict(version_id, payload.action)
    if version is None:
        return {"ok": False, "message": message, "version": None}
    return {"ok": True, "message": message, "version": version}


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"实验方案 {entry_id} 不存在")
    return entry
