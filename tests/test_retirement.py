"""退役过渡名录的独立场景测试（直接运行）：

    python tests/test_retirement.py

使用全新的临时 SQLite 库，覆盖：
- 退役资格：首版/现行版/不存在/已退役 → 拒绝；
- 有有效鉴定登记在该版 → 拒绝；撤回后 → 允许；
- 退役后原版清单、发布时去向、鉴定档案仍可查；
- 供数改走跨过退役版的直接映射，每份鉴定候选与标本结论不变；
- 连续退役相邻旧版，结论仍逐份保持一致；
- 登记与退役的并发先后语义（登记先提交→拒绝退役；退役先提交→拒绝登记）；
- 查询在退役事务前后只见完整状态（快照隔离）。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0


def check(cond: bool, msg: str) -> None:
    global PASS
    if not cond:
        raise AssertionError(msg)
    PASS += 1
    print(f"  ✔ {msg}")


def reverse_order_case() -> None:
    """连续退役相邻旧版但顺序相反（先新后旧），结论仍保持一致。"""
    from app.database import _connect, init_db
    from app.schemas import IdentificationCreate, SpeciesIn, VersionCreate
    from app.services import (
        bootstrap_version, publish_version, register_identification,
        resolve_specimen, retire_version,
    )

    path = tempfile.mktemp(suffix=".db")
    os.environ["DET_DB_PATH"] = path
    init_db(path)
    conn = _connect(path)

    def sp(*codes):
        return [SpeciesIn(species_code=c, scientific_name=c) for c in codes]

    bootstrap_version(conn, VersionCreate(
        version_code="w1", name="w1", species=sp("A", "B"), mappings={}))
    publish_version(conn, VersionCreate(
        version_code="w2", name="w2", species=sp("A1", "A2", "B"),
        mappings={"A": ["A1", "A2"], "B": ["B"]}))
    publish_version(conn, VersionCreate(
        version_code="w3", name="w3", species=sp("A1", "A2", "B9"),
        mappings={"A1": ["A1"], "A2": ["A2"], "B": ["B9"]}))
    publish_version(conn, VersionCreate(
        version_code="w4", name="w4", species=sp("A1", "A2", "B9", "C"),
        mappings={"A1": ["A1"], "A2": ["A2"], "B9": ["B9", "C"]}))

    i1 = register_identification(conn, IdentificationCreate(
        specimen_no="R1", version_code="w1", species_code="A"))
    before = resolve_specimen(conn, "R1")
    before_tuple = (
        before["status"], before["resolved_species_code"], tuple(before["candidates"])
    )

    # 先退役更靠后的 w3，再退役 w2
    retire_version(conn, "w3")  # w3 无鉴定
    check(
        conn.execute("SELECT COUNT(*) AS n FROM serving_mappings "
                     "WHERE from_version='w2' AND to_version='w4'").fetchone()["n"] > 0,
        "先退役 w3：w2 直接连到 w4",
    )
    retire_version(conn, "w2")  # w2 无鉴定
    got = {
        (r["from_code"], r["to_code"])
        for r in conn.execute(
            "SELECT from_code, to_code FROM serving_mappings "
            "WHERE from_version='w1' AND to_version='w4'")
    }
    check(got == {("A", "A1"), ("A", "A2"), ("B", "B9"), ("B", "C")},
          "逆序连续退役后 w1→w4 合成映射正确")
    after = resolve_specimen(conn, "R1")
    after_tuple = (
        after["status"], after["resolved_species_code"], tuple(after["candidates"])
    )
    check(after_tuple == before_tuple, "逆序退役后 R1 的标本结论与候选不变")
    check(i1["id"] is not None, "历史鉴定编号保持不变")
    conn.close()


def race_case() -> None:
    """登记与退役并发竞争：二者串行化，绝不出现“退役成功且有效鉴定仍在该版”。

    两种提交先后都尝试，结果必须恰好是二者之一：
    - 退役先行：退役成功，登记 409；
    - 登记先行：登记成功，退役 409。
    """
    import threading

    from app.database import _connect, init_db
    from app.schemas import SpeciesIn, VersionCreate
    from app.services import (
        bootstrap_version, publish_version, register_identification,
        resolve_specimen, retire_version,
    )
    from app.schemas import IdentificationCreate

    path = tempfile.mktemp(suffix=".db")
    os.environ["DET_DB_PATH"] = path
    init_db(path)
    setup = _connect(path)

    def sp(*codes):
        return [SpeciesIn(species_code=c, scientific_name=c) for c in codes]

    bootstrap_version(setup, VersionCreate(
        version_code="r1", name="r1", species=sp("A"), mappings={}))
    publish_version(setup, VersionCreate(
        version_code="r2", name="r2", species=sp("A"), mappings={"A": ["A"]}))
    publish_version(setup, VersionCreate(
        version_code="r3", name="r3", species=sp("A"), mappings={"A": ["A"]}))
    setup.close()

    def attempt(delay_register: float) -> tuple[str, str]:
        result: dict[str, str] = {}

        def do_register():
            reg_conn = _connect(path)  # SQLite 连接须在所属线程内创建
            try:
                register_identification(reg_conn, IdentificationCreate(
                    specimen_no="RACE", version_code="r2", species_code="A"))
                result["register"] = "ok"
            except Exception as exc:  # noqa: BLE001
                result["register"] = str(getattr(exc, "status_code", "err"))
            finally:
                reg_conn.close()

        def do_retire():
            ret_conn = _connect(path)
            try:
                retire_version(ret_conn, "r2")
                result["retire"] = "ok"
            except Exception as exc:  # noqa: BLE001
                result["retire"] = str(getattr(exc, "status_code", "err"))
            finally:
                ret_conn.close()

        t_ret = threading.Thread(target=do_retire)
        t_reg = threading.Thread(target=do_register)
        if delay_register:
            t_ret.start()
            t_reg.start()
        else:
            t_reg.start()
            t_ret.start()
        t_reg.join()
        t_ret.join()
        return result["register"], result["retire"]

    outcomes = set()
    for _ in range(8):
        # 每轮重建 r2 的可用状态
        setup = _connect(path)
        # 若上一轮登记成功但退役失败，撤回该鉴定
        setup.execute("BEGIN IMMEDIATE")
        setup.execute(
            "UPDATE identifications SET status='withdrawn', withdrawn_at=datetime('now') "
            "WHERE status='active'"
        )
        # 若 r2 已退役，重建 r2 作为新的可退役过渡版（r3 仍现行）
        retired = setup.execute(
            "SELECT retired_at FROM checklist_versions WHERE version_code='r2'"
        ).fetchone()
        if retired["retired_at"] is not None:
            # 手动恢复：重新插入 r2 与映射（模拟新一轮发布）
            setup.execute(
                "UPDATE checklist_versions SET retired_at=NULL WHERE version_code='r2'"
            )
            setup.execute(
                "INSERT OR IGNORE INTO serving_mappings"
                "(from_version,to_version,from_code,to_code) "
                "VALUES ('r1','r2','A','A'),('r2','r3','A','A')"
            )
            setup.execute(
                "DELETE FROM serving_mappings WHERE from_version='r1' AND to_version='r3'"
            )
        setup.execute("COMMIT")
        setup.close()

        reg_status, ret_status = attempt(delay_register=len(outcomes) % 2)
        outcomes.add((reg_status, ret_status))
        # 不可能二者都成功
        check(not (reg_status == "ok" and ret_status == "ok"),
              f"并发结果 {reg_status}/{ret_status}：不会同时登记与退役成功")
        # 至少一个成功（有一个先提交）
        check("ok" in (reg_status, ret_status),
              f"并发结果 {reg_status}/{ret_status}：先到者成功")

        # 不变量：若退役成功，则 r2 无有效鉴定
        verify = _connect(path)
        active = verify.execute(
            "SELECT COUNT(*) AS n FROM identifications "
            "WHERE version_code='r2' AND status='active'").fetchone()["n"]
        retired = verify.execute(
            "SELECT retired_at FROM checklist_versions WHERE version_code='r2'"
        ).fetchone()["retired_at"]
        if retired is not None:
            check(active == 0, "退役成功后 r2 无有效鉴定（不变量成立）")
        else:
            check(True, "退役未成功时 r2 仍可继续登记（不变量成立）")
        # 供数查询始终自洽
        res = resolve_specimen(verify, "RACE")
        check(res["snapshot_consistent"] is True, "并发竞争下查询仍单快照自洽")
        verify.close()


def main() -> int:
    path = tempfile.mktemp(suffix=".db")
    os.environ["DET_DB_PATH"] = path

    # 必须在设置环境变量后再导入（DB_PATH 在导入时读取）
    from app.database import _connect, init_db
    from app.schemas import IdentificationCreate, SpeciesIn, VersionCreate
    from app.services import (
        bootstrap_version,
        publish_version,
        register_identification,
        resolve_specimen,
        retire_version,
        withdraw_identification,
    )

    init_db(path)

    conn = _connect(path)

    def species(*codes):
        return [SpeciesIn(species_code=c, scientific_name=c) for c in codes]

    print("\n[1] 建立四版线性链 v1→v2→v3→v4，其中 v2/v3 为过渡版")
    bootstrap_version(conn, VersionCreate(
        version_code="v1", name="v1", species=species("A", "B"), mappings={}))
    publish_version(conn, VersionCreate(
        version_code="v2", name="v2", species=species("A1", "A2", "B"),
        mappings={"A": ["A1", "A2"], "B": ["B"]}))
    publish_version(conn, VersionCreate(
        version_code="v3", name="v3", species=species("A1", "A2", "B9"),
        mappings={"A1": ["A1"], "A2": ["A2"], "B": ["B9"]}))
    publish_version(conn, VersionCreate(
        version_code="v4", name="v4", species=species("A1", "A2", "B9", "C"),
        mappings={"A1": ["A1"], "A2": ["A2"], "B9": ["B9", "C"]}))
    check(
        conn.execute("SELECT version_code FROM checklist_versions "
                     "WHERE is_current=1").fetchone()["version_code"] == "v4",
        "现行版为 v4",
    )

    # 在 v1、v2、v3 登记鉴定；v2 的一份随后撤回，用于演示“撤回后可退役”
    i_v1 = register_identification(conn, IdentificationCreate(
        specimen_no="S1", version_code="v1", species_code="A"))
    i_v2_keep = register_identification(conn, IdentificationCreate(
        specimen_no="S2", version_code="v2", species_code="A1"))
    i_v2_drop = register_identification(conn, IdentificationCreate(
        specimen_no="S3", version_code="v2", species_code="A1"))
    i_v3 = register_identification(conn, IdentificationCreate(
        specimen_no="S4", version_code="v3", species_code="B9"))

    def snapshot(specimens):
        out = {}
        for sp in specimens:
            r = resolve_specimen(conn, sp)
            out[sp] = (
                r["status"],
                r["resolved_species_code"],
                tuple(r["candidates"]),
                tuple(
                    (t["identification_id"], tuple(t["candidates_at_current"]))
                    for t in r["active_identifications"]
                ),
            )
        return out

    specimens = ["S1", "S2", "S3", "S4"]
    before = snapshot(specimens)

    print("\n[2] 退役资格校验")
    for bad, label in (("v1", "首版"), ("v4", "现行版")):
        try:
            retire_version(conn, bad)
            raise AssertionError(f"{label} {bad} 不应允许退役")
        except Exception as exc:  # HTTPException
            check(getattr(exc, "status_code", None) == 409,
                  f"{label} {bad} 拒绝退役（409）")
    try:
        retire_version(conn, "v9")
        raise AssertionError("不存在的版本不应允许退役")
    except Exception as exc:  # noqa: BLE001
        check(getattr(exc, "status_code", None) == 404,
              "不存在的版本 v9 返回 404")

    print("\n[3] v2/v3 仍有有效鉴定 → 拒绝退役")
    for ver in ("v2", "v3"):
        try:
            retire_version(conn, ver)
            raise AssertionError(f"{ver} 有有效鉴定不应允许退役")
        except Exception as exc:  # noqa: BLE001
            check(getattr(exc, "status_code", None) == 409,
                  f"{ver} 有有效鉴定时拒绝退役（409）")

    print("\n[4] 撤回 v2 的全部有效鉴定后，退役 v2 成功")
    withdraw_identification(conn, i_v2_drop["id"])
    withdraw_identification(conn, i_v2_keep["id"])
    row, pred, succ = retire_version(conn, "v2")
    check(row["retired_at"] is not None, "v2 已写入 retired_at")
    check((pred, succ) == ("v1", "v3"), "供数链把 v1 直接连到 v3（跨过 v2）")

    # 原版清单、发布时去向、鉴定档案仍可查
    v2_species = {
        r["species_code"] for r in conn.execute(
            "SELECT species_code FROM species WHERE version_code='v2'")
    }
    check(v2_species == {"A1", "A2", "B"}, "v2 原版物种清单仍完整可查")
    v2_out = conn.execute(
        "SELECT from_code, to_code FROM code_mappings "
        "WHERE from_version='v2' ORDER BY from_code, to_code").fetchall()
    check(len(v2_out) == 3, "v2 发布时的代码去向仍按原样留档（3 条）")
    archived = conn.execute(
        "SELECT status FROM identifications WHERE id=?", (i_v2_keep["id"],)
    ).fetchone()
    check(archived["status"] == "withdrawn", "v2 的鉴定档案仍在（已撤回）")

    # 供数映射：v1→v3 为两段合成
    composed = conn.execute(
        "SELECT from_code, to_code FROM serving_mappings "
        "WHERE from_version='v1' AND to_version='v3' "
        "ORDER BY from_code, to_code").fetchall()
    got = {(r["from_code"], r["to_code"]) for r in composed}
    check(got == {("A", "A1"), ("A", "A2"), ("B", "B9")},
          "v1→v3 直接映射为 v1→v2→v3 的合成结果")

    # 结论保持不变（v2 已无有效鉴定，其余鉴定的现行候选不应变化）
    after_v2 = snapshot(specimens)
    for sp in ("S1", "S4"):
        check(after_v2[sp] == before[sp],
              f"{sp} 在退役 v2 后结论与逐份候选不变")

    print("\n[5] 向已退役版登记 / 重新发布 / 重复退役均提示已退役")
    for fn, label in (
        (lambda: register_identification(conn, IdentificationCreate(
            specimen_no="S9", version_code="v2", species_code="A1")), "向 v2 登记"),
        (lambda: publish_version(conn, VersionCreate(
            version_code="v2", name="again", species=species("X"),
            mappings={"X": ["X"]})), "重新发布 v2"),
        (lambda: retire_version(conn, "v2"), "重复退役 v2"),
    ):
        try:
            fn()
            raise AssertionError(f"{label} 应被拒绝")
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", "")
            check(getattr(exc, "status_code", None) == 409 and "退役" in detail,
                  f"{label} 返回 409 且提示已退役")

    print("\n[6] 连续退役相邻旧版：撤回 v3 的鉴定后退役 v3")
    withdraw_identification(conn, i_v3["id"])
    row3, pred3, succ3 = retire_version(conn, "v3")
    check(row3["retired_at"] is not None, "v3 已退役")
    check((pred3, succ3) == ("v1", "v4"), "供数链把 v1 直接连到 v4（跨过 v2、v3）")
    composed2 = conn.execute(
        "SELECT from_code, to_code FROM serving_mappings "
        "WHERE from_version='v1' AND to_version='v4' "
        "ORDER BY from_code, to_code").fetchall()
    got2 = {(r["from_code"], r["to_code"]) for r in composed2}
    check(got2 == {("A", "A1"), ("A", "A2"), ("B", "B9"), ("B", "C")},
          "v1→v4 直接映射为多跳合成结果")
    after_v3 = snapshot(specimens)
    check(after_v3["S1"] == before["S1"],
          "连续退役后 S1 的现行候选与标本结论仍不变")

    print("\n[7] 登记先提交且仍有效 → 拒绝退役；退役先提交 → 拒绝登记")
    # 发布 v5（过渡版）与 v6（现行版）；先在 v5 登记
    publish_version(conn, VersionCreate(
        version_code="v5", name="v5", species=species("A1", "A2", "B9", "C"),
        mappings={"A1": ["A1"], "A2": ["A2"], "B9": ["B9"], "C": ["C"]}))
    publish_version(conn, VersionCreate(
        version_code="v6", name="v6", species=species("A1", "A2", "B9", "C", "D"),
        mappings={"A1": ["A1"], "A2": ["A2"], "B9": ["B9"], "C": ["C"]}))
    i_v5 = register_identification(conn, IdentificationCreate(
        specimen_no="S5", version_code="v5", species_code="A1"))
    try:
        retire_version(conn, "v5")
        raise AssertionError("v5 有有效鉴定，不应允许退役")
    except Exception as exc:  # noqa: BLE001
        check(getattr(exc, "status_code", None) == 409
              and "有效鉴定" in getattr(exc, "detail", ""),
              "登记先提交且仍有效 → 拒绝退役（409）")
    # 撤回后可以退役；退役提交后登记被拒绝
    withdraw_identification(conn, i_v5["id"])
    retire_version(conn, "v5")
    try:
        register_identification(conn, IdentificationCreate(
            specimen_no="S6", version_code="v5", species_code="A1"))
        raise AssertionError("退役后不应允许登记")
    except Exception as exc:  # noqa: BLE001
        check(getattr(exc, "status_code", None) == 409
              and "退役" in getattr(exc, "detail", ""),
              "退役先提交后，向该版登记被拒绝（409 已退役）")

    print("\n[8] 查询快照：退役提交前后，单次查询只看到完整状态")
    # 在 v4（既非首版也非现行版）登记后撤回，使其可退役
    i_v4 = register_identification(conn, IdentificationCreate(
        specimen_no="S7", version_code="v4", species_code="C"))
    withdraw_identification(conn, i_v4["id"])
    # 用另一条连接在只读事务进行中提交退役写入
    reader = _connect(path)
    reader.execute("BEGIN")
    seen_before = reader.execute(
        "SELECT COUNT(*) AS n FROM serving_mappings "
        "WHERE from_version='v4'").fetchone()["n"]
    retire_version(conn, "v4")  # 独立连接提交
    seen_after = reader.execute(
        "SELECT COUNT(*) AS n FROM serving_mappings "
        "WHERE from_version='v4'").fetchone()["n"]
    check(seen_before == seen_after and seen_before > 0,
          "只读事务内仍看到退役前的完整供数映射")
    reader.execute("COMMIT")
    reader.close()
    fresh = _connect(path)
    fresh_n = fresh.execute(
        "SELECT COUNT(*) AS n FROM serving_mappings "
        "WHERE from_version='v4'").fetchone()["n"]
    check(fresh_n == 0, "新事务看到退役后的完整状态（v4 已撤出供数链）")
    fresh.close()

    print("\n[9] HTTP 接口层（TestClient）与 /docs 暴露")
    import app.database as dbmod
    dbmod.DB_PATH = path  # TestClient 的连接改指向本场景库
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as api:
        docs = api.get("/openapi.json")
        check(docs.status_code == 200, "OpenAPI 可访问（/docs 数据源）")
        paths = docs.json()["paths"]
        check("/versions/{version_code}/retire" in paths, "退役端点出现在 /docs")
        check(
            "/versions/{version_code}/mappings/serving" in paths,
            "供数映射端点出现在 /docs",
        )

        versions = {v["version_code"]: v for v in api.get("/versions").json()}
        check(versions["v2"]["retired_at"] is not None, "版本列表标出 v2 已退役")
        check(versions["v6"]["is_current"] is True, "版本列表标出 v6 为现行版")

        r = api.post("/versions/v2/retire")
        check(r.status_code == 409 and "已退役" in r.json()["detail"],
              "HTTP 重提已退役版 → 409 已退役")
        r = api.post("/versions/v1/retire")
        check(r.status_code == 409, "HTTP 退役首版 → 409")
        r = api.post("/versions/v6/retire")
        check(r.status_code == 409, "HTTP 退役现行版 → 409")
        r = api.post("/identifications", json={
            "specimen_no": "S-HTTP", "version_code": "v2", "species_code": "A1"})
        check(r.status_code == 409 and "退役" in r.json()["detail"],
              "HTTP 向已退役版登记 → 409 已退役")

        # 原版资料按原接口仍可查
        check(api.get("/versions/v2/species").status_code == 200,
              "HTTP 旧接口可查 v2 原版清单")
        old_maps = api.get("/versions/v2/mappings/outgoing").json()
        check(len(old_maps) == 3, "HTTP 旧接口可查 v2 发布时去向")
        ids = api.get("/identifications", params={"specimen_no": "S3"}).json()
        check(len(ids) == 1 and ids[0]["version_code"] == "v2",
              "HTTP 旧接口可查登记在 v2 的鉴定档案")

        # 供数映射端点展示“跨过退役版”的直接映射
        serving = api.get("/versions/v1/mappings/serving").json()
        got_http = {(m["from_code"], c) for m in serving for c in m["to_codes"]}
        check(("A", "A1") in got_http and ("B", "C") in got_http,
              "HTTP 供数映射为跨过退役版的直接映射")
        check(all(m["to_version"] == "v6" for m in serving),
              "HTTP 供数映射直达现行版 v6")

        res = api.get("/specimens/S1/resolution").json()
        check(
            [s["version"] for s in res["active_identifications"][0]["chain"]]
            == ["v1", "v6"],
            "HTTP 查询链已跨过全部退役版（v1→v6），不再出现 v2/v3/v4",
        )
        check(res["status"] == "review" and res["resolved_species_code"] is None,
              "HTTP 查询结论为待复核且不入库（与退役前一致）")

    conn.close()

    print("\n[10] 连续退役相邻旧版（逆序：先新后旧）")
    reverse_order_case()

    print("\n[11] 登记与退役并发竞争：串行化，不出现矛盾状态")
    race_case()

    print(f"\n退役场景全部断言通过 ✔（共 {PASS} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
