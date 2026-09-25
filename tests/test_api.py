"""API 端到端测试：每个用例使用独立的临时 SQLite 数据库。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_sample(client, name):
    r = client.post("/samples", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_pool(client, name, recipe):
    r = client.post("/pools", json={"name": name, "recipe": recipe})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fractions_of(client, batch_id):
    r = client.get(f"/batches/{batch_id}/composition")
    assert r.status_code == 200, r.text
    return {
        c["sample_id"]: (c["numerator"], c["denominator"])
        for c in r.json()["components"]
    }


def test_single_sample_composition(client):
    s1 = make_sample(client, "S1")
    assert fractions_of(client, s1) == {s1: (1, 1)}


def test_pool_composition_is_reduced(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 2:2 投入，结果应约分为 1/2 + 1/2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 2},
    ])
    assert fractions_of(client, p1) == {s1: (1, 2), s2: (1, 2)}


def test_multilevel_same_source_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    # P2 = 1×P1 + 1×S1：S1 经两条路径到达，须合并为 1/2·1/2 + 1/2 = 3/4
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    assert fractions_of(client, p2) == {s1: (3, 4), s2: (1, 4)}


def test_deep_chain(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 = 3×P2 + 1×S2 → S1 = 3/4·3/4 = 9/16，S2 = 3/4·1/4 + 1/4 = 7/16
    p3 = make_pool(client, "P3", [
        {"batch_id": p2, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert fractions_of(client, p3) == {s1: (9, 16), s2: (7, 16)}


def test_duplicate_recipe_entries_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ]})
    assert r.status_code == 201
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    assert fractions_of(client, body["id"]) == {s1: (3, 4), s2: (1, 4)}


def test_empty_recipe_rejected(client):
    r = client.post("/pools", json={"name": "P1", "recipe": []})
    assert r.status_code == 422


def test_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ]})
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": portions},
    ]})
    assert r.status_code == 422


def test_query_unknown_batch_returns_404(client):
    assert client.get("/batches/4242/composition").status_code == 404
    assert client.get("/batches/4242").status_code == 404


def test_duplicate_name_rejected(client):
    make_sample(client, "S1")
    r = client.post("/samples", json={"name": "S1"})
    assert r.status_code == 409


def test_pool_can_only_reference_existing_batches(client):
    # 引用尚未创建的批次（未来的 id）同样被拒绝
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1 + 100, "portions": 1},
    ]})
    assert r.status_code == 400


def preview(client, batch_id, recipe):
    return client.post(f"/batches/{batch_id}/recipe-preview", json={"recipe": recipe})


def preview_components(batch):
    """{(sample_id): ((原分子, 原分母), (预演分子, 预演分母), (差值分子, 差值分母))}"""
    return {
        c["sample_id"]: (
            (c["original"]["numerator"], c["original"]["denominator"]),
            (c["preview"]["numerator"], c["preview"]["denominator"]),
            (c["delta"]["numerator"], c["delta"]["denominator"]),
        )
        for c in batch["components"]
    }


def test_recipe_preview_basic(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # 把 P1 的配方预演替换为 3×S1 + 1×S2
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == p1
    assert body["batch_name"] == "P1"
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    # 受影响批次 = 目标自身 + 下游 P2，按 id 升序、各出现一次
    assert [b["batch_id"] for b in body["affected_batches"]] == [p1, p2]
    b1, b2 = body["affected_batches"]
    assert preview_components(b1) == {
        s1: ((1, 2), (3, 4), (1, 4)),
        s2: ((1, 2), (1, 4), (-1, 4)),
    }
    # P2' = 1/2·P1' + 1/2·S1 → S1 = 7/8，S2 = 1/8
    assert preview_components(b2) == {
        s1: ((3, 4), (7, 8), (1, 8)),
        s2: ((1, 4), (1, 8), (-1, 8)),
    }


def test_recipe_preview_merges_duplicate_ingredients(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]


def test_recipe_preview_multi_path_merged_and_deduped(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 经 P1、P2 两条路径汇合到 P1；与 P1 无关的 P4 不应受影响
    p3 = make_pool(client, "P3", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": p2, "portions": 1},
    ])
    s3 = make_sample(client, "S3")
    p4 = make_pool(client, "P4", [{"batch_id": s3, "portions": 1}])

    # 把 P1 预演替换为纯 S2
    r = preview(client, p1, [{"batch_id": s2, "portions": 1}])
    assert r.status_code == 200, r.text
    affected = {b["batch_id"]: b for b in r.json()["affected_batches"]}
    assert set(affected) == {p1, p2, p3}  # 多路径汇合，同一批次只返回一次

    # P1' = S2；P2' = 1/2·P1' + 1/2·S1；P3' = 1/2·P1' + 1/2·P2'
    assert preview_components(affected[p1]) == {
        s1: ((1, 2), (0, 1), (-1, 2)),
        s2: ((1, 2), (1, 1), (1, 2)),
    }
    assert preview_components(affected[p2]) == {
        s1: ((3, 4), (1, 2), (-1, 4)),
        s2: ((1, 4), (1, 2), (1, 4)),
    }
    assert preview_components(affected[p3]) == {
        s1: ((5, 8), (1, 4), (-3, 8)),
        s2: ((3, 8), (3, 4), (3, 8)),
    }


def test_recipe_preview_does_not_modify_database(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    before_detail = client.get(f"/batches/{p1}").json()
    before_list = client.get("/batches").json()
    before_comp_p1 = fractions_of(client, p1)
    before_comp_p2 = fractions_of(client, p2)

    r = preview(client, p1, [{"batch_id": s2, "portions": 5}])
    assert r.status_code == 200, r.text

    # 预演后详情、列表与占比查询结果保持原样
    assert client.get(f"/batches/{p1}").json() == before_detail
    assert client.get("/batches").json() == before_list
    assert fractions_of(client, p1) == before_comp_p1
    assert fractions_of(client, p2) == before_comp_p2


def test_recipe_preview_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ])
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


def test_recipe_preview_ingredient_must_predate_target(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    s2 = make_sample(client, "S2")  # 比 P1 更晚创建，不能作为 P1 的预演原料
    r = preview(client, p1, [{"batch_id": s2, "portions": 1}])
    assert r.status_code == 400
    assert str(s2) in r.json()["detail"]
    # 自引用同样会形成环，被拒绝
    r = preview(client, p1, [{"batch_id": p1, "portions": 1}])
    assert r.status_code == 400


def test_recipe_preview_sample_target_rejected(client):
    s1 = make_sample(client, "S1")
    r = preview(client, s1, [{"batch_id": s1, "portions": 1}])
    assert r.status_code == 400


def test_recipe_preview_unknown_target_returns_404(client):
    assert preview(client, 4242, [{"batch_id": 1, "portions": 1}]).status_code == 404


def test_recipe_preview_empty_recipe_rejected(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    assert preview(client, p1, []).status_code == 422


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_recipe_preview_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    assert preview(client, p1, [{"batch_id": s1, "portions": portions}]).status_code == 422


def plan(client, a, b, sample, num, den, max_total=100):
    return client.post("/pool-plans", json={
        "ingredient_a_id": a,
        "ingredient_b_id": b,
        "sample_id": sample,
        "target_numerator": num,
        "target_denominator": den,
        "max_total_portions": max_total,
    })


def test_pool_plan_basic_and_recipe_directly_submittable(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # S1 在原料中的占比为 1 与 0，目标 1/4 → x/y = (1/4-0)/(1-1/4) = 1/3
    r = plan(client, s1, s2, s1, 1, 4)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 1},
        {"batch_id": s2, "batch_name": "S2", "portions": 3},
    ]
    assert body["total_portions"] == 4
    assert {(c["sample_id"], c["numerator"], c["denominator"]) for c in body["composition"]} == {
        (s1, 1, 4),
        (s2, 3, 4),
    }

    # 返回的配方可直接提交 /pools，创建出的混样占比与规划结果一致
    created = make_pool(client, "PX", body["recipe"])
    assert fractions_of(client, created) == {s1: (1, 4), s2: (3, 4)}


def test_pool_plan_multilevel_ingredients(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    # P1 中 S1 占 1/2，S2 中占 0，目标 1/3 → x/y = (1/3-0)/(1/2-1/3) = 2/1
    r = plan(client, p1, s2, s1, 1, 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
        {"batch_id": p1, "batch_name": "P1", "portions": 2},
    ]
    assert body["total_portions"] == 3
    assert {(c["sample_id"], c["numerator"], c["denominator"]) for c in body["composition"]} == {
        (s1, 1, 3),
        (s2, 2, 3),
    }


def test_pool_plan_both_ingredients_at_target_gives_one_to_one(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": s1, "portions": 3},
        {"batch_id": s2, "portions": 3},
    ])
    # 两种原料中 S1 占比都是 1/2，目标也是 1/2 → 约定取 1:1
    r = plan(client, p1, p2, s1, 1, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": p1, "batch_name": "P1", "portions": 1},
        {"batch_id": p2, "batch_name": "P2", "portions": 1},
    ]
    assert body["total_portions"] == 2


def test_pool_plan_target_zero(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # 两种原料都不含 S3，目标 0 → 任意比例均可，取 1:1
    r = plan(client, s1, s2, s3, 0, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_portions"] == 2
    assert s3 not in {c["sample_id"] for c in body["composition"]}
    # 只有一种原料不含目标样本时，0 无法由正整数份数达到
    assert plan(client, s1, s2, s1, 0, 1).status_code == 422


def test_pool_plan_unachievable_returns_422(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])  # S1 占 1/2
    p2 = make_pool(client, "P2", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 3},
    ])  # S1 占 1/4
    # 目标超出两种原料占比的范围（两侧都不行）
    assert plan(client, p1, p2, s1, 3, 4).status_code == 422
    assert plan(client, p1, p2, s1, 1, 8).status_code == 422
    # 目标恰等于其中一种原料的占比（另一个须投入 0 份，非正整数解）
    assert plan(client, p1, p2, s1, 1, 2).status_code == 422
    assert plan(client, p1, p2, s1, 1, 4).status_code == 422
    # 两种原料占比相同但目标不同
    p3 = make_pool(client, "P3", [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 2},
    ])  # S1 占 1/2
    assert plan(client, p1, p3, s1, 1, 3).status_code == 422


def test_pool_plan_min_solution_exceeds_max_returns_422(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 最小解为 1+3=4 份：上限 3 → 422，上限 4 → 200
    r = plan(client, s1, s2, s1, 1, 4, max_total=3)
    assert r.status_code == 422
    assert "4" in r.json()["detail"] and "3" in r.json()["detail"]
    assert plan(client, s1, s2, s1, 1, 4, max_total=4).status_code == 200


def test_pool_plan_does_not_create_batch(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    before = client.get("/batches").json()
    r = plan(client, s1, s2, s1, 1, 4)
    assert r.status_code == 200, r.text
    assert client.get("/batches").json() == before


def test_pool_plan_same_ingredient_rejected(client):
    s1 = make_sample(client, "S1")
    assert plan(client, s1, s1, s1, 1, 2).status_code == 422


@pytest.mark.parametrize("num,den", [(2, 1), (1, 0), (-1, 2), (1.5, 2), ("1", 2)])
def test_pool_plan_invalid_target_ratio_rejected(client, num, den):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    assert plan(client, s1, s2, s1, num, den).status_code == 422


@pytest.mark.parametrize("max_total", [0, -1, 1.5, "10", True])
def test_pool_plan_invalid_max_total_rejected(client, max_total):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    assert plan(client, s1, s2, s1, 1, 2, max_total=max_total).status_code == 422


def test_pool_plan_unknown_or_wrong_kind_rejected(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    # 未知原料 / 未知样本 → 400
    assert plan(client, s1, 999, s1, 1, 2).status_code == 400
    assert plan(client, s1, s2, 999, 1, 2).status_code == 400
    # 目标样本不是原始样本 → 400
    r = plan(client, s1, s2, p1, 1, 2)
    assert r.status_code == 400
    assert str(p1) in r.json()["detail"]
