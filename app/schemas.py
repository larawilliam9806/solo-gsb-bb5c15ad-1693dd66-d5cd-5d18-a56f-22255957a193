"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# 严格的正整数：拒绝 0、负数、浮点数（如 1.5 / 2.0）与字符串（如 "3"）
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]

BatchName = Annotated[str, Field(min_length=1, max_length=200)]


class SampleCreate(BaseModel):
    name: BatchName


class RecipeItem(BaseModel):
    batch_id: StrictPositiveInt
    portions: StrictPositiveInt


class PoolCreate(BaseModel):
    name: BatchName
    recipe: Annotated[list[RecipeItem], Field(min_length=1)]  # 拒绝空配方


class RecipeItemOut(BaseModel):
    batch_id: int
    batch_name: str
    portions: int


class BatchOut(BaseModel):
    id: int
    name: BatchName
    kind: Literal["sample", "pool"]
    created_at: str
    recipe: list[RecipeItemOut] | None = None  # 仅混样批次返回配方


class ComponentOut(BaseModel):
    sample_id: int
    sample_name: str
    numerator: int    # 约分后的分子
    denominator: int  # 约分后的分母


class CompositionOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    kind: Literal["sample", "pool"]
    components: list[ComponentOut]


class RecipePreviewRequest(BaseModel):
    """完整替代配方（与创建混样相同：非空、严格正整数份数、重复原料合并）。"""

    recipe: Annotated[list[RecipeItem], Field(min_length=1)]  # 拒绝空配方


class FractionOut(BaseModel):
    numerator: int    # 约分后的分子（有符号）
    denominator: int  # 约分后的分母（恒为正）


class PreviewComponentOut(BaseModel):
    sample_id: int
    sample_name: str
    original: FractionOut
    preview: FractionOut
    delta: FractionOut  # 有符号差值 = preview - original


class AffectedBatchOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    kind: Literal["sample", "pool"]
    components: list[PreviewComponentOut]


class RecipePreviewOut(BaseModel):
    batch_id: int
    batch_name: BatchName
    recipe: list[RecipeItemOut]  # 合并重复原料后的替代配方
    affected_batches: list[AffectedBatchOut]  # 目标批次 + 全部下游批次，各只出现一次
