"""因子约束分配接口：整组生成分配表、按明细回执、增量重算与事务核对。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import PageResult
from app.services.factor_allocation import (
    DETAIL_FIELDS,
    STRATEGY_REBUILD,
    STRATEGY_REUSE,
    FactorAllocationService,
)

router = APIRouter(prefix="/api/factor_allocation", tags=["因子约束分配"])

service = FactorAllocationService()

LIST_FIELDS = [
    "实验编号", "实验名称", "因子编号", "因子名称", "因子层级",
    "因子组", "缓存批次", "规则版本", "生成状态",
]
STATUSES = ["已回执", "待复核"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("/status")
def get_status() -> dict[str, Any]:
    """批次总览：在册批次、缓存条数与配置版本。"""
    return service.status()


@router.get("/preview")
def preview() -> dict[str, Any]:
    """只读预览：列出分组、校验结果与缓存命中，不落库。"""
    return service.preview()


@router.get("/conflict")
def get_conflict() -> dict[str, Any]:
    """只读检测：缓存刷新与提交之间是否存在版本冲突，及需要补算的实验。"""
    return service.detect_conflict()


@router.post("/generate")
def generate(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """整组一次提交生成分配表，多条明细逐条回执。

    payload 支持：scope=full|incremental|retry、strategy=reuse|rebuild、
    dry_run=true、simulate_failure=因子组名（中断演练）。
    """
    values = payload or {}
    scope = str(values.get("scope") or "full")
    strategy = str(values.get("strategy") or STRATEGY_REUSE)
    try:
        return service.generate(
            scope=scope,
            strategy=strategy,
            dry_run=bool(values.get("dry_run")),
            simulate_failure=values.get("simulate_failure"),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/retry")
def retry(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """批次中断后按未完成组补算；已完成组的明细与缓存批次保持不变。"""
    values = payload or {}
    return service.generate(
        scope="retry",
        strategy=STRATEGY_REUSE,
        simulate_failure=values.get("simulate_failure"),
    )


@router.post("/rebuild")
def rebuild(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """清空缓存后整批重建：旧批次与旧缓存差异留档，重建后保留差异可核对。"""
    values = payload or {}
    return service.generate(
        scope="full",
        strategy=STRATEGY_REBUILD,
        simulate_failure=values.get("simulate_failure"),
    )


@router.get("/verify")
def verify() -> dict[str, Any]:
    """核对分配表与约束缓存是否一一对齐，确认没有半套事务残留。"""
    return service.verify()


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    items = service.list_batches()
    return {"items": items, "total": len(items)}


@router.get("/cache")
def list_cache() -> dict[str, Any]:
    """约束缓存明细：含缓存批次、规则版本与失效标记。"""
    return service.list_cache()


@router.get("/archive")
def list_archive() -> dict[str, Any]:
    """整批重建的留档：旧批次明细与旧缓存差异。"""
    items = service.list_archive()
    return {"items": items, "total": len(items)}


@router.get("/consumer")
def consumer_view(
    batch_no: str | None = Query(default=None, description="不传则读取在册批次"),
) -> dict[str, Any]:
    """指标定义模块读取分配表：实验编号、缓存批次与因子层。"""
    return service.consumer_view(batch_no=batch_no)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    state: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=200, ge=1, le=1000000),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_allocations(
        filters=query_filters(request), keyword=keyword, state=state, page=page, size=size)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *DETAIL_FIELDS])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in DETAIL_FIELDS)]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="factor_allocation.csv"'})


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按实验编号或因子编号检索"),
    state: str | None = Query(default=None, description="已回执、待复核"),
    batch_no: str | None = Query(default=None, description="不传则读取在册批次"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、关键字与回执状态过滤因子约束分配明细。"""
    items, total = service.list_allocations(
        filters=query_filters(request), keyword=keyword, state=state,
        page=page, size=size, batch_no=batch_no)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分配明细：回显所在批次与命中的约束缓存。"""
    entry = service.get_allocation(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分配明细 {entry_id} 不存在或已归档")
    return entry
