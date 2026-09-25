"""实验室混样批次追溯 API。

- POST /samples                        登记原始样本
- POST /pools                          创建混样批次（引用已有批次 + 正整数份数）
- POST /pool-plans                     规划混样配方（反算达到目标占比的最小份数，不写库）
- GET  /batches                        列出全部批次
- GET  /batches/{batch_id}             批次详情（混样含配方）
- GET  /batches/{batch_id}/composition 递归计算原始样本占比（约分后的分子/分母）
- POST /batches/{batch_id}/recipe-preview 配方变更影响预演（内存计算，不写库）
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from fractions import Fraction

from fastapi import Depends, FastAPI, HTTPException, status

from . import db
from .schemas import (
    AffectedBatchOut,
    BatchOut,
    ComponentOut,
    CompositionOut,
    FractionOut,
    PoolCreate,
    PoolPlanOut,
    PoolPlanRequest,
    PreviewComponentOut,
    RecipeItemOut,
    RecipePreviewOut,
    RecipePreviewRequest,
    SampleCreate,
)
from .service import batch_composition, plan_portions, recipe_preview


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动时自动建表
    yield


app = FastAPI(
    title="实验室混样批次追溯 API",
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _fetch_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _batch_out(conn: sqlite3.Connection, row: sqlite3.Row) -> BatchOut:
    recipe = None
    if row["kind"] == "pool":
        recipe = [
            RecipeItemOut(
                batch_id=r["ingredient_id"],
                batch_name=r["name"],
                portions=r["portions"],
            )
            for r in conn.execute(
                """
                SELECT pr.ingredient_id, b.name, pr.portions
                FROM pool_recipes pr
                JOIN batches b ON b.id = pr.ingredient_id
                WHERE pr.pool_id = ?
                ORDER BY pr.ingredient_id
                """,
                (row["id"],),
            )
        ]
    return BatchOut(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        created_at=row["created_at"],
        recipe=recipe,
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/samples",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记原始样本",
)
def create_sample(payload: SampleCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    try:
        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'sample')", (payload.name,)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, cur.lastrowid))


@app.post(
    "/pools",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建混样批次",
)
def create_pool(payload: PoolCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    # 同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    found = {
        row["id"]
        for row in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    try:
        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'pool')", (payload.name,)
        )
        pool_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO pool_recipes (pool_id, ingredient_id, portions) VALUES (?, ?, ?)",
            [(pool_id, i, merged[i]) for i in ids],
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, pool_id))


@app.post(
    "/pool-plans",
    response_model=PoolPlanOut,
    summary="规划混样配方（只规划，不创建批次）",
)
def plan_pool(payload: PoolPlanRequest, conn: sqlite3.Connection = Depends(get_db)) -> PoolPlanOut:
    ids = [payload.ingredient_a_id, payload.ingredient_b_id, payload.sample_id]
    info = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, name, kind FROM batches WHERE id IN (?, ?, ?)", ids
        )
    }
    missing = [i for i in ids if i not in info]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )
    if info[payload.sample_id]["kind"] != "sample":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"不是原始样本: {payload.sample_id}",
        )

    # 沿用现有精确占比计算，取目标样本在两种原料中的占比（原料不含该样本时视为 0）
    comp_a = batch_composition(conn, payload.ingredient_a_id)
    comp_b = batch_composition(conn, payload.ingredient_b_id)
    target = Fraction(payload.target_numerator, payload.target_denominator)
    share_a = comp_a.get(payload.sample_id, Fraction(0))
    share_b = comp_b.get(payload.sample_id, Fraction(0))

    solution = plan_portions(share_a, share_b, target)
    if solution is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"目标占比 {target} 无法由正整数份数达到: "
                f"目标样本在两种原料中的占比为 {share_a} 与 {share_b}"
            ),
        )
    portions_a, portions_b = solution
    total = portions_a + portions_b
    if total > payload.max_total_portions:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"最小总份数 {total} 超过上限 {payload.max_total_portions}",
        )

    portions_by_id = {
        payload.ingredient_a_id: portions_a,
        payload.ingredient_b_id: portions_b,
    }

    # 新混样的全部原始样本占比：按份数加权合并两种原料的占比
    merged: dict[int, Fraction] = {}
    for ingredient_id, portions in portions_by_id.items():
        share = Fraction(portions, total)
        composition = comp_a if ingredient_id == payload.ingredient_a_id else comp_b
        for sample_id, frac in composition.items():
            merged[sample_id] = merged.get(sample_id, Fraction(0)) + share * frac

    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in merged),
            list(merged),
        )
    }
    return PoolPlanOut(
        recipe=[
            RecipeItemOut(
                batch_id=i, batch_name=info[i]["name"], portions=portions_by_id[i]
            )
            for i in sorted(portions_by_id)
        ],
        total_portions=total,
        composition=[
            ComponentOut(
                sample_id=sample_id,
                sample_name=names[sample_id],
                numerator=frac.numerator,
                denominator=frac.denominator,
            )
            for sample_id, frac in sorted(merged.items())
        ],
    )


@app.get("/batches", response_model=list[BatchOut], summary="列出全部批次")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> list[BatchOut]:
    rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    return [_batch_out(conn, row) for row in rows]


@app.get("/batches/{batch_id}", response_model=BatchOut, summary="批次详情")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    return _batch_out(conn, row)


@app.get(
    "/batches/{batch_id}/composition",
    response_model=CompositionOut,
    summary="递归计算原始样本占比",
)
def get_composition(
    batch_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> CompositionOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")

    composition = batch_composition(conn, batch_id)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in composition),
            list(composition),
        )
    }
    components = [
        ComponentOut(
            sample_id=sample_id,
            sample_name=names[sample_id],
            numerator=frac.numerator,
            denominator=frac.denominator,
        )
        for sample_id, frac in sorted(composition.items())
    ]
    return CompositionOut(
        batch_id=row["id"],
        batch_name=row["name"],
        kind=row["kind"],
        components=components,
    )


@app.post(
    "/batches/{batch_id}/recipe-preview",
    response_model=RecipePreviewOut,
    summary="配方变更影响预演（不修改数据库）",
)
def preview_recipe(
    batch_id: int,
    payload: RecipePreviewRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> RecipePreviewOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    if row["kind"] != "pool":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原始样本没有配方可替换: {batch_id}",
        )

    # 与创建混样一致：同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    found = {
        r["id"]
        for r in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    # 避免预演成环：原料只能引用比目标批次更早创建（id 更小）的批次
    too_late = [i for i in ids if i >= batch_id]
    if too_late:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原料只能引用比批次 {batch_id} 更早创建的批次: {too_late}",
        )

    affected = recipe_preview(conn, batch_id, merged)

    # 一次性解析受影响批次、涉及样本与替代配方原料的名称/类型
    involved = set(affected) | set(ids)
    for original, preview in affected.values():
        involved.update(original)
        involved.update(preview)
    info = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, kind FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in involved),
            sorted(involved),
        )
    }

    def frac_out(frac: Fraction) -> FractionOut:
        return FractionOut(numerator=frac.numerator, denominator=frac.denominator)

    batches_out = []
    for bid, (original, preview) in affected.items():
        components = [
            PreviewComponentOut(
                sample_id=sample_id,
                sample_name=info[sample_id]["name"],
                original=frac_out(original.get(sample_id, Fraction(0))),
                preview=frac_out(preview.get(sample_id, Fraction(0))),
                delta=frac_out(
                    preview.get(sample_id, Fraction(0))
                    - original.get(sample_id, Fraction(0))
                ),
            )
            for sample_id in sorted(set(original) | set(preview))
        ]
        batches_out.append(
            AffectedBatchOut(
                batch_id=bid,
                batch_name=info[bid]["name"],
                kind=info[bid]["kind"],
                components=components,
            )
        )

    return RecipePreviewOut(
        batch_id=row["id"],
        batch_name=row["name"],
        recipe=[
            RecipeItemOut(batch_id=i, batch_name=info[i]["name"], portions=merged[i])
            for i in ids
        ],
        affected_batches=batches_out,
    )
