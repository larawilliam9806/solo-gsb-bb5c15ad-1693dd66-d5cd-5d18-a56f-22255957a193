"""混样批次的原始样本占比计算。

全程使用 fractions.Fraction 做精确有理数运算，避免浮点误差；
Fraction 会自动约分，分子/分母即为最简形式。
"""
from __future__ import annotations

import sqlite3
from fractions import Fraction

Composition = dict[int, Fraction]  # {原始样本 id: 占比}
Recipes = dict[int, list[tuple[int, int]]]  # {混样批次 id: [(原料批次 id, 份数)]}


def _load_graph(conn: sqlite3.Connection) -> tuple[dict[int, str], Recipes]:
    """读取全部批次类型与配方，构成内存中的配方引用图。"""
    kinds = {row["id"]: row["kind"] for row in conn.execute("SELECT id, kind FROM batches")}

    recipes: Recipes = {}
    for row in conn.execute("SELECT pool_id, ingredient_id, portions FROM pool_recipes"):
        recipes.setdefault(row["pool_id"], []).append((row["ingredient_id"], row["portions"]))
    return kinds, recipes


def _expand(
    kinds: dict[int, str],
    recipes: Recipes,
    batch_id: int,
    memo: dict[int, Composition],
) -> Composition:
    """递归展开批次；memo 让多路径汇合的批次只精确计算一次。"""
    if batch_id in memo:
        return memo[batch_id]
    if kinds[batch_id] == "sample":
        memo[batch_id] = {batch_id: Fraction(1)}
        return memo[batch_id]
    items = recipes.get(batch_id, [])
    total = sum(portions for _, portions in items)
    result: Composition = {}
    for ingredient_id, portions in items:
        share = Fraction(portions, total)
        for sample_id, frac in _expand(kinds, recipes, ingredient_id, memo).items():
            result[sample_id] = result.get(sample_id, Fraction(0)) + share * frac
    memo[batch_id] = result
    return result


def batch_composition(conn: sqlite3.Connection, batch_id: int) -> Composition:
    """递归展开批次，返回 {原始样本 id: 占比}。

    - 每一层按“投入份数 / 该层总份数”分摊占比；
    - 同一原始样本经多条路径到达时，占比相加合并；
    - 新批次只能引用已创建的批次，引用关系天然是有向无环图，递归必然终止。
    """
    kinds, recipes = _load_graph(conn)
    return _expand(kinds, recipes, batch_id, {})


def downstream_batches(recipes: Recipes, batch_id: int) -> list[int]:
    """沿配方引用关系找出全部下游批次（含目标自身）。

    下游 = 直接或间接把目标批次当作原料的混样批次；
    多路径汇合时同一批次只收集一次，结果按 id 升序返回。
    """
    consumers: dict[int, list[int]] = {}
    for pool_id, items in recipes.items():
        for ingredient_id, _ in items:
            consumers.setdefault(ingredient_id, []).append(pool_id)

    seen = {batch_id}
    stack = [batch_id]
    while stack:
        for consumer in consumers.get(stack.pop(), []):
            if consumer not in seen:
                seen.add(consumer)
                stack.append(consumer)
    return sorted(seen)


def recipe_preview(
    conn: sqlite3.Connection, batch_id: int, new_recipe: dict[int, int]
) -> dict[int, tuple[Composition, Composition]]:
    """在内存中把 batch_id 的配方替换为 new_recipe，评估影响，不修改数据库。

    返回 {受影响批次 id: (原占比, 预演占比)}；新配方只允许引用比目标批次
    更早创建的批次（由调用方校验），替换后引用图仍是无环图，递归必然终止。
    """
    kinds, recipes = _load_graph(conn)
    affected = downstream_batches(recipes, batch_id)

    preview_recipes = dict(recipes)
    preview_recipes[batch_id] = sorted(new_recipe.items())

    original_memo: dict[int, Composition] = {}
    preview_memo: dict[int, Composition] = {}
    return {
        bid: (
            _expand(kinds, recipes, bid, original_memo),
            _expand(kinds, preview_recipes, bid, preview_memo),
        )
        for bid in affected
    }
