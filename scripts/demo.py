"""端到端演示：拆种、并种、撤回、换版失败、退役过渡版。

用法（服务启动后）：
    python scripts/demo.py
    BASE_URL=http://localhost:8000 python scripts/demo.py

脚本只调用 HTTP 接口，不直接读库；每步都打印请求与结论。
版本号与标本号都带运行后缀，因此在 Docker Compose 的命名卷上可**重复运行**：
若库中已有版本，会在现行版之上先发布一个演示基线，再演示拆种/并种/退役。
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
RUN = time.strftime("%H%M%S")

SPLIT = f"SPLIT-{RUN}"
MERGE = f"MERGE-{RUN}"
CONFL = f"CONFL-{RUN}"
V1B = f"v1-{RUN}"       # 演示基线（库非空时）
V2 = f"v2-{RUN}"
V3 = f"v3-{RUN}"

# 演示用的固定物种代码与换版故事
STORY_BASE = ["PIN01", "PIN02", "QRC01", "QRC02", "CUN01"]
V1_SPECIES = [
    {"species_code": "PIN01", "scientific_name": "Pinus massoniana"},
    {"species_code": "PIN02", "scientific_name": "Pinus tabuliformis"},
    {"species_code": "QRC01", "scientific_name": "Quercus variabilis"},
    {"species_code": "QRC02", "scientific_name": "Quercus acutissima"},
    {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
]
V2_SPECIES = [
    {"species_code": "PIN01A", "scientific_name": "Pinus massoniana var. A"},
    {"species_code": "PIN01B", "scientific_name": "Pinus massoniana var. B"},
    {"species_code": "PIN02", "scientific_name": "Pinus tabuliformis"},
    {"species_code": "QRC99", "scientific_name": "Quercus sensu lato"},
    {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
]
V2_MAP = {
    "PIN01": ["PIN01A", "PIN01B"],  # 拆种：一源多去向
    "PIN02": ["PIN02"],
    "QRC01": ["QRC99"],             # 并种：两源同去向
    "QRC02": ["QRC99"],
    "CUN01": ["CUN01"],
}
V3_SPECIES = [
    {"species_code": "PIN01A", "scientific_name": "Pinus massoniana var. A"},
    {"species_code": "PIN01B", "scientific_name": "Pinus massoniana var. B"},
    {"species_code": "PIN99", "scientific_name": "Pinus sensu lato"},
    {"species_code": "QRC99X", "scientific_name": "Quercus sl. subsp. X"},
    {"species_code": "QRC99Y", "scientific_name": "Quercus sl. subsp. Y"},
    {"species_code": "CUN01", "scientific_name": "Cunninghamia lanceolata"},
]
V3_MAP = {
    "PIN01A": ["PIN01A"],
    "PIN01B": ["PIN01B"],
    "PIN02": ["PIN99"],
    "QRC99": ["QRC99X", "QRC99Y"],  # 再拆
    "CUN01": ["CUN01"],
}


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def show(resp: httpx.Response) -> dict:
    data = resp.json()
    print(f"-> HTTP {resp.status_code}: {data}")
    return data


def resolve(client: httpx.Client, specimen: str) -> dict:
    r = client.get(f"{BASE_URL}/specimens/{specimen}/resolution")
    return show(r)


def _post_version(client, version_code, species, mappings):
    return show(client.post(f"{BASE_URL}/versions", json={
        "version_code": version_code,
        "name": f"演示版本 {version_code}",
        "species": species,
        "mappings": mappings,
    }))


def add_baseline(client, version_code):
    """在现有现行版之上加一个基线：保留全部旧代码，并补齐故事代码（恒等映射）。"""
    current = client.get(f"{BASE_URL}/versions/current").json()["current_version"]
    prev = client.get(f"{BASE_URL}/versions/{current}/species").json()
    merged = {sp["species_code"]: sp for sp in prev}
    for sp in V1_SPECIES:
        merged.setdefault(sp["species_code"], sp)
    mappings = {sp["species_code"]: [sp["species_code"]] for sp in prev}
    return _post_version(
        client, version_code, [merged[c] for c in sorted(merged)], mappings
    )


def compose_version(client, version_code, species, story_map):
    """在现行版之上发布故事版本：故事代码用指定去向，其余代码恒等保留。"""
    current = client.get(f"{BASE_URL}/versions/current").json()["current_version"]
    prev = client.get(f"{BASE_URL}/versions/{current}/species").json()
    prev_codes = {sp["species_code"] for sp in prev}
    mappings = {c: story_map.get(c, [c]) for c in sorted(prev_codes)}
    merged = {sp["species_code"]: sp for sp in prev}
    for sp in species:
        merged[sp["species_code"]] = sp
    targets = {t for v in mappings.values() for t in v}
    for t in targets:
        merged.setdefault(t, {"species_code": t, "scientific_name": t})
    return _post_version(
        client, version_code, [merged[c] for c in sorted(targets)], mappings
    )


def main() -> int:
    client = httpx.Client(timeout=10)
    banner("0. 健康检查 / 现行版本")
    show(client.get(f"{BASE_URL}/health"))
    cur = show(client.get(f"{BASE_URL}/versions/current"))

    # 选一个演示基线：空库直接 bootstrap v1；非空库在现行版之上发布带后缀的基线
    if cur["current_version"] is None:
        print("未初始化，先 bootstrap v1 ...")
        show(client.post(f"{BASE_URL}/versions/bootstrap", json={
            "version_code": "v1", "name": "第一版树木名录（初始版）",
            "species": V1_SPECIES, "mappings": {},
        }))
        base_v1 = "v1"
    else:
        print(f"库中已有版本（现行 {cur['current_version']}），"
              f"发布演示基线 {V1B}（保留旧代码，补齐故事代码）...")
        add_baseline(client, V1B)
        base_v1 = V1B
    base_codes = {
        sp["species_code"]
        for sp in client.get(f"{BASE_URL}/versions/{base_v1}/species").json()
    }
    missing = [c for c in STORY_BASE if c not in base_codes]
    if missing:
        raise SystemExit(f"演示基线缺少故事代码 {missing}，请换一个空库或清理卷后重试")

    banner(f"1. 在 {base_v1} 登记三份标本的鉴定")
    id_split = show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": SPLIT, "version_code": base_v1, "species_code": "PIN01",
    }))["id"]
    id_merge = show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": MERGE, "version_code": base_v1, "species_code": "PIN02",
    }))["id"]
    show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": CONFL, "version_code": base_v1, "species_code": "QRC01",
    }))
    id_confl_qrc02 = show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": CONFL, "version_code": base_v1, "species_code": "QRC02",
    }))["id"]

    print(f"\n-- 初始结论（{base_v1}，均应为预期状态）--")
    r1 = resolve(client, SPLIT)
    assert r1["status"] == "unique" and r1["resolved_species_code"] == "PIN01"
    r2 = resolve(client, MERGE)
    assert r2["status"] == "unique" and r2["resolved_species_code"] == "PIN02"
    r3 = resolve(client, CONFL)
    print(f"   {CONFL} 两份不同鉴定 -> 状态 {r3['status']}（{r3['status_label']}）")
    assert r3["status"] == "conflict"

    banner(f"2. 发布 {V2}：PIN01 拆种，QRC01+QRC02 并种，逐代码给出去向")
    compose_version(client, V2, V2_SPECIES, V2_MAP)
    print("\n-- 换版后重新查询 --")
    r1 = resolve(client, SPLIT)
    print(f"   {SPLIT}: {r1['status']}（{r1['status_label']}）候选={r1['candidates']}")
    assert r1["status"] == "review" and set(r1["candidates"]) == {"PIN01A", "PIN01B"}
    r2 = resolve(client, MERGE)
    print(f"   {MERGE}: {r2['status']}（{r2['status_label']}）入库代码={r2['resolved_species_code']}")
    assert r2["status"] == "unique" and r2["resolved_species_code"] == "PIN02"
    r3 = resolve(client, CONFL)
    print(f"   {CONFL}: {r3['status']}（{r3['status_label']}）候选={r3['candidates']}")
    assert r3["status"] == "unique" and r3["resolved_species_code"] == "QRC99", "并种应消除原冲突"

    banner("3. 换版失败演练：故意漏掉一个前版代码的去向")
    cur = client.get(f"{BASE_URL}/versions/current").json()["current_version"]
    prev = client.get(f"{BASE_URL}/versions/{cur}/species").json()
    identity = {sp["species_code"]: [sp["species_code"]] for sp in prev}
    drop = sorted(identity)[-1]
    bad_map = {k: v for k, v in identity.items() if k != drop}
    show(client.post(f"{BASE_URL}/versions", json={
        "version_code": f"vbad-{RUN}", "name": "缺映射的坏版本",
        "species": prev, "mappings": bad_map,
    }))
    cur2 = show(client.get(f"{BASE_URL}/versions/current"))
    assert cur2["current_version"] == V2, "失败后必须仍按原名录供数"
    print(f"   现行版仍为 {V2}，查询不受影响：")
    assert resolve(client, SPLIT)["status"] == "review"

    banner(f"4. 发布 {V3}（多跳转换）：QRC99 再拆、PIN02 并入")
    compose_version(client, V3, V3_SPECIES, V3_MAP)
    r3c = resolve(client, CONFL)
    print(f"   {CONFL} 经 {base_v1}→{V2}→{V3} 两跳：{r3c['status']} 候选={r3c['candidates']}")
    assert r3c["status"] == "review"
    assert set(r3c["candidates"]) == {"QRC99X", "QRC99Y"}
    print("   转换链明细：")
    for tr in r3c["active_identifications"]:
        print("    ", tr["identification_id"], tr["chain"])

    banner("5. 撤回鉴定，观察结论变化")
    show(client.post(f"{BASE_URL}/identifications/{id_confl_qrc02}/withdraw"))
    r3c = resolve(client, CONFL)
    print(f"   撤回一份后 {CONFL}: {r3c['status']}（{r3c['status_label']}）候选={r3c['candidates']}")
    assert r3c["status"] == "review"  # 剩 QRC01 -> QRC99 -> 两个候选
    print("   重复撤回应报错：")
    show(client.post(f"{BASE_URL}/identifications/{id_confl_qrc02}/withdraw"))

    show(client.post(f"{BASE_URL}/identifications/{id_split}/withdraw"))
    r1 = resolve(client, SPLIT)
    print(f"   撤回唯一鉴定后 {SPLIT}: {r1['status']}（{r1['status_label']}）")
    assert r1["status"] == "unidentified" and r1["candidates"] == []

    banner(f"6. 换版期间登记：用现行版 {V3} 代码登记，立即查询")
    new_id = show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": SPLIT, "version_code": V3, "species_code": "PIN99",
    }))["id"]
    r1 = resolve(client, SPLIT)
    assert r1["status"] == "unique" and r1["resolved_species_code"] == "PIN99"
    print(f"   {SPLIT} 重新鉴定为 PIN99 -> {r1['status']}，入库代码 {r1['resolved_species_code']}")

    banner("7. 非法登记校验（版本/代码不存在）")
    show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": SPLIT, "version_code": "v9", "species_code": "X",
    }))
    show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": SPLIT, "version_code": V3, "species_code": "NOPE",
    }))

    banner("8. 退役过渡名录：撤出日常取数链，原资料留档")
    cur = show(client.get(f"{BASE_URL}/versions/current"))["current_version"]
    cur_species = show(client.get(f"{BASE_URL}/versions/{cur}/species"))
    ident = {sp["species_code"]: [sp["species_code"]] for sp in cur_species}
    vT = f"vT-{RUN}"
    vN = f"vN-{RUN}"
    show(client.post(f"{BASE_URL}/versions", json={
        "version_code": vT, "name": f"过渡版 {vT}", "species": cur_species,
        "mappings": ident,
    }))
    show(client.post(f"{BASE_URL}/versions", json={
        "version_code": vN, "name": f"现行版 {vN}", "species": cur_species,
        "mappings": ident,
    }))
    before = resolve(client, CONFL)
    print(f"   {CONFL} 在 {vN} 下的结论：{before['status']} 候选={before['candidates']}")

    print(f"\n-- 在过渡版 {vT} 登记一份鉴定，再尝试退役 --")
    target = cur_species[0]["species_code"]
    trans_id = show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": f"RET-{RUN}", "version_code": vT, "species_code": target,
    }))["id"]
    blocked = show(client.post(f"{BASE_URL}/versions/{vT}/retire"))
    assert blocked.get("detail") and "有效鉴定" in blocked["detail"], "有有效鉴定须拒绝退役"
    print("   退役失败，供数链与转换照旧：")
    assert resolve(client, CONFL)["candidates"] == before["candidates"]

    print(f"\n-- 撤回后退役 {vT} --")
    show(client.post(f"{BASE_URL}/identifications/{trans_id}/withdraw"))
    retired = show(client.post(f"{BASE_URL}/versions/{vT}/retire"))
    assert retired["retired_at"]
    print(f"   原版清单仍可查：{len(client.get(f'{BASE_URL}/versions/{vT}/species').json())} 个代码")
    print(f"   发布时去向仍可查：{len(client.get(f'{BASE_URL}/versions/{vT}/mappings/outgoing').json())} 条")
    archived = client.get(f"{BASE_URL}/identifications",
                          params={"specimen_no": f"RET-{RUN}"}).json()
    assert archived and archived[0]["withdrawn_at"]
    print(f"   鉴定档案仍可查：编号 {archived[0]['id']}（{archived[0]['status']}）")

    served = show(client.get(f"{BASE_URL}/versions/{cur}/mappings/serving"))
    assert served and all(m["to_version"] == vN for m in served), "供数直连到现行版"
    print(f"   日常供数已改为 {cur} → {vN} 的直接映射（跨过 {vT}）")

    after = resolve(client, CONFL)
    assert after["status"] == before["status"], "标本结论保持一致"
    assert after["candidates"] == before["candidates"], "每份鉴定的现行候选保持一致"
    print(f"   退役后 {CONFL} 结论不变：{after['status']} 候选={after['candidates']}")

    print("\n-- 重提已退役版 / 向已退役版登记 --")
    again = show(client.post(f"{BASE_URL}/versions", json={
        "version_code": vT, "name": "重提", "species": cur_species, "mappings": ident,
    }))
    assert "退役" in again.get("detail", ""), "重提已退役版须告知已退役"
    reg = show(client.post(f"{BASE_URL}/identifications", json={
        "specimen_no": f"RET2-{RUN}", "version_code": vT, "species_code": target,
    }))
    assert "退役" in reg.get("detail", ""), "向已退役版登记须告知已退役"

    banner("全部断言通过 ✔  演示标本号前缀：SPLIT/MERGE/CONFL/RET-" + RUN)
    print(f"可在 {BASE_URL}/docs 继续手工试验；鉴定编号示例：{id_merge}, {new_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
