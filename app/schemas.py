"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# 严格的正整数：拒绝 0、负数、浮点数（如 1.5 / 2.0）与字符串（如 "3"）
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]

# 严格的非负整数：同样拒绝浮点数与字符串
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

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


class PoolPlanRequest(BaseModel):
    """混样规划请求：两种已有原料 + 已有原始样本 + 目标占比 + 总份数上限。"""

    ingredient_a_id: StrictPositiveInt
    ingredient_b_id: StrictPositiveInt
    sample_id: StrictPositiveInt
    target_numerator: StrictNonNegativeInt  # 目标占比分子
    target_denominator: StrictPositiveInt   # 目标占比分母
    max_total_portions: StrictPositiveInt   # 总份数上限

    @model_validator(mode="after")
    def _check_plan(self) -> PoolPlanRequest:
        if self.ingredient_a_id == self.ingredient_b_id:
            raise ValueError("两种原料批次必须不同")
        if self.target_numerator > self.target_denominator:
            raise ValueError("目标占比需满足 0 ≤ 分子 ≤ 分母")
        return self


class PoolPlanOut(BaseModel):
    recipe: list[RecipeItemOut]      # 两项配方，可直接作为 /pools 的 recipe 提交
    total_portions: int              # 最小解的总份数
    composition: list[ComponentOut]  # 新混样全部原始样本占比（约分后）


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
