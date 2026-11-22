"""业务逻辑：名录发布、鉴定登记/撤回、候选物种解析。"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from .schemas import (
    STATUS_CONFLICT,
    STATUS_LABELS,
    STATUS_REVIEW,
    STATUS_UNIDENTIFIED,
    STATUS_UNIQUE,
    IdentificationCreate,
    VersionCreate,
)


# ---------- 名录版本 ----------

def get_current_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT version_code FROM checklist_versions WHERE is_current = 1 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return row["version_code"] if row else None


def get_first_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT version_code FROM checklist_versions ORDER BY rowid LIMIT 1"
    ).fetchone()
    return row["version_code"] if row else None


def list_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT v.*, "
        "(SELECT COUNT(*) FROM species s WHERE s.version_code = v.version_code) "
        "AS species_count "
        "FROM checklist_versions v ORDER BY v.rowid"
    ).fetchall()


def publish_version(conn: sqlite3.Connection, payload: VersionCreate) -> None:
    """原子发布新版名录。

    校验不通过或写入出错时整体回滚，旧版继续供数（见 main.py 异常处理）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev = get_current_version(conn)
        if prev is None:
            raise HTTPException(
                status_code=409,
                detail="尚未存在任何名录，首个版本请使用初始化接口 /versions/bootstrap",
            )
        exists = conn.execute(
            "SELECT retired_at FROM checklist_versions WHERE version_code = ?",
            (payload.version_code,),
        ).fetchone()
        if exists:
            if exists["retired_at"] is not None:
                raise HTTPException(
                    409,
                    f"版号 {payload.version_code} 已退役，不能重新发布；"
                    "如需恢复请使用新的版号",
                )
            raise HTTPException(409, f"版号 {payload.version_code} 已存在")

        prev_codes = {
            r["species_code"]
            for r in conn.execute(
                "SELECT species_code FROM species WHERE version_code = ?", (prev,)
            )
        }
        new_codes = {sp.species_code for sp in payload.species}
        if len(new_codes) != len(payload.species):
            raise HTTPException(400, "新版物种清单中存在重复的物种代码")

        # 1) 前版每个代码都必须给出一个或多个去向
        missing = sorted(prev_codes - set(payload.mappings))
        if missing:
            raise HTTPException(
                400,
                f"前版代码缺少去向映射: {missing}；前版每个代码都须给出新版的一个或多个去向",
            )
        extra = sorted(set(payload.mappings) - prev_codes)
        if extra:
            raise HTTPException(400, f"映射中存在不属于前版 {prev} 的代码: {extra}")
        for src, targets in payload.mappings.items():
            if not targets:
                raise HTTPException(400, f"代码 {src} 的去向为空，至少需要一个去向")
            unknown = sorted(set(targets) - new_codes)
            if unknown:
                raise HTTPException(
                    400, f"代码 {src} 的去向 {unknown} 不在新版物种清单中"
                )

        # 2) 写入新版本与物种
        conn.execute(
            "INSERT INTO checklist_versions"
            "(version_code, name, parent_version, is_current) VALUES (?,?,?,0)",
            (payload.version_code, payload.name, prev),
        )
        conn.executemany(
            "INSERT INTO species(version_code, species_code, scientific_name) "
            "VALUES (?,?,?)",
            [
                (payload.version_code, sp.species_code, sp.scientific_name)
                for sp in payload.species
            ],
        )
        # 3) 写入逐代码去向（拆种：一源多目标；并种：多源指向同一目标）
        rows = [
            (prev, payload.version_code, src, dst)
            for src, targets in payload.mappings.items()
            for dst in targets
        ]
        conn.executemany(
            "INSERT INTO code_mappings"
            "(from_version, to_version, from_code, to_code) VALUES (?,?,?,?)",
            rows,
        )
        # 发布时的映射同时成为日常供数链的一环
        conn.executemany(
            "INSERT INTO serving_mappings"
            "(from_version, to_version, from_code, to_code) VALUES (?,?,?,?)",
            rows,
        )
        # 4) 切换现行版指针
        conn.execute("UPDATE checklist_versions SET is_current = 0")
        conn.execute(
            "UPDATE checklist_versions SET is_current = 1 WHERE version_code = ?",
            (payload.version_code,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def bootstrap_version(conn: sqlite3.Connection, payload: VersionCreate) -> None:
    """初始化第一个名录版本（无前置版本，不要求映射）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if get_current_version(conn) is not None:
            raise HTTPException(409, "名录已初始化，请使用 /versions 发布新版")
        existing = conn.execute(
            "SELECT retired_at FROM checklist_versions WHERE version_code = ?",
            (payload.version_code,),
        ).fetchone()
        if existing:
            if existing["retired_at"] is not None:
                raise HTTPException(
                    409, f"版号 {payload.version_code} 已退役，请使用新的版号"
                )
            raise HTTPException(409, f"版号 {payload.version_code} 已存在")
        codes = [sp.species_code for sp in payload.species]
        if len(codes) != len(set(codes)):
            raise HTTPException(400, "物种代码存在重复")
        conn.execute(
            "INSERT INTO checklist_versions"
            "(version_code, name, parent_version, is_current) VALUES (?,?,NULL,1)",
            (payload.version_code, payload.name),
        )
        conn.executemany(
            "INSERT INTO species(version_code, species_code, scientific_name) "
            "VALUES (?,?,?)",
            [
                (payload.version_code, sp.species_code, sp.scientific_name)
                for sp in payload.species
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def retire_version(
    conn: sqlite3.Connection, version_code: str
) -> tuple[sqlite3.Row, str, str]:
    """按版号退役一个过渡名录版本（原子事务）。

    仅受理既非首版、也非现行版，且没有有效鉴定登记在该版的版本。
    退役只撤出日常供数链：原版清单、发布时的代码去向与鉴定档案全部保留；
    日常供数改用跨过该版的直接映射（serving_mappings 中把前驱→该版→后继
    两段映射合成一段），因此同一现行版下每份鉴定的候选保持不变。

    并发语义：本函数与登记同为 BEGIN IMMEDIATE 写事务。
    - 登记先提交且仍有效 → 本函数看到有效鉴定并拒绝退役；
    - 退役先提交 → 后续登记看到 retired_at 并拒绝。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM checklist_versions WHERE version_code = ?",
            (version_code,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"名录版本 {version_code} 不存在")
        if row["retired_at"] is not None:
            raise HTTPException(409, f"版号 {version_code} 已退役，不能重复退役")

        first = get_first_version(conn)
        current = get_current_version(conn)
        if version_code == first:
            raise HTTPException(409, f"版号 {version_code} 是首版，不能退役")
        if version_code == current:
            raise HTTPException(409, f"版号 {version_code} 是现行版，不能退役")

        active = conn.execute(
            "SELECT COUNT(*) AS n FROM identifications "
            "WHERE version_code = ? AND status = 'active'",
            (version_code,),
        ).fetchone()["n"]
        if active:
            raise HTTPException(
                409,
                f"版号 {version_code} 仍有 {active} 份有效鉴定登记在该版，不能退役；"
                "请先撤回这些鉴定",
            )

        # serving 链上该版唯一的前驱与后继（线性链，拆种会产生多行但版号唯一）
        preds = [
            r["from_version"]
            for r in conn.execute(
                "SELECT DISTINCT from_version FROM serving_mappings "
                "WHERE to_version = ?",
                (version_code,),
            )
        ]
        succs = [
            r["to_version"]
            for r in conn.execute(
                "SELECT DISTINCT to_version FROM serving_mappings "
                "WHERE from_version = ?",
                (version_code,),
            )
        ]
        if len(preds) != 1 or len(succs) != 1:
            raise HTTPException(
                500,
                f"供数链结构异常：{version_code} 的前驱={preds} 后继={succs}",
            )
        pred, succ = preds[0], succs[0]

        # 前驱→该版、该版→后继 两段合成 前驱→后继 的直接映射
        to_succ: dict[str, set[str]] = {}
        for r in conn.execute(
            "SELECT from_code, to_code FROM serving_mappings "
            "WHERE from_version = ? AND to_version = ?",
            (version_code, succ),
        ):
            to_succ.setdefault(r["from_code"], set()).add(r["to_code"])

        composed: set[tuple[str, str]] = set()
        for r in conn.execute(
            "SELECT from_code, to_code FROM serving_mappings "
            "WHERE from_version = ? AND to_version = ?",
            (pred, version_code),
        ):
            targets = to_succ.get(r["to_code"])
            if not targets:
                raise HTTPException(
                    500,
                    f"供数映射链断裂：{version_code} 代码 {r['to_code']} "
                    f"在 {succ} 无去向（数据异常）",
                )
            for dst in targets:
                composed.add((r["from_code"], dst))
        if not composed:
            raise HTTPException(500, f"退役 {version_code} 时无法合成有效映射")

        conn.execute(
            "DELETE FROM serving_mappings "
            "WHERE (from_version = ? AND to_version = ?) "
            "   OR (from_version = ? AND to_version = ?)",
            (pred, version_code, version_code, succ),
        )
        conn.executemany(
            "INSERT INTO serving_mappings"
            "(from_version, to_version, from_code, to_code) VALUES (?,?,?,?)",
            [(pred, succ, src, dst) for src, dst in sorted(composed)],
        )
        conn.execute(
            "UPDATE checklist_versions SET retired_at = datetime('now') "
            "WHERE version_code = ?",
            (version_code,),
        )
        conn.execute("COMMIT")
        retired = conn.execute(
            "SELECT * FROM checklist_versions WHERE version_code = ?",
            (version_code,),
        ).fetchone()
        return retired, pred, succ
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 鉴定登记 / 撤回 ----------

def register_identification(
    conn: sqlite3.Connection, payload: IdentificationCreate
) -> sqlite3.Row:
    conn.execute("BEGIN IMMEDIATE")
    try:
        ver = conn.execute(
            "SELECT retired_at FROM checklist_versions WHERE version_code = ?",
            (payload.version_code,),
        ).fetchone()
        if not ver:
            raise HTTPException(404, f"名录版本 {payload.version_code} 不存在")
        if ver["retired_at"] is not None:
            raise HTTPException(
                409,
                f"名录版本 {payload.version_code} 已退役，不能向该版登记鉴定",
            )
        sp = conn.execute(
            "SELECT 1 FROM species WHERE version_code = ? AND species_code = ?",
            (payload.version_code, payload.species_code),
        ).fetchone()
        if not sp:
            raise HTTPException(
                404,
                f"物种代码 {payload.species_code} 不属于版本 {payload.version_code}",
            )
        cur = conn.execute(
            "INSERT INTO identifications(specimen_no, version_code, species_code) "
            "VALUES (?,?,?)",
            (payload.specimen_no, payload.version_code, payload.species_code),
        )
        conn.execute("COMMIT")
        new_id = cur.lastrowid
        return conn.execute(
            "SELECT * FROM identifications WHERE id = ?", (new_id,)
        ).fetchone()
    except Exception:
        conn.execute("ROLLBACK")
        raise


def withdraw_identification(conn: sqlite3.Connection, identification_id: int) -> sqlite3.Row:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM identifications WHERE id = ?", (identification_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"鉴定编号 {identification_id} 不存在")
        if row["status"] == "withdrawn":
            raise HTTPException(409, f"鉴定编号 {identification_id} 已撤回，不能重复撤回")
        conn.execute(
            "UPDATE identifications SET status='withdrawn', "
            "withdrawn_at=datetime('now') WHERE id=?",
            (identification_id,),
        )
        conn.execute("COMMIT")
        return conn.execute(
            "SELECT * FROM identifications WHERE id = ?", (identification_id,)
        ).fetchone()
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 查询解析 ----------

def _children_chain(conn: sqlite3.Connection, start_version: str) -> list[tuple[str, str]]:
    """从登记版本出发，沿日常供数链取到现行版的各段映射 (from_version, to_version)。

    供数链由 serving_mappings 表达：未退役相邻版之间就是发布时的映射；
    某版退役后，其前驱与后继直接相连，因此这里自然跨过退役版。
    """
    links: list[tuple[str, str]] = []
    cur = start_version
    seen = {cur}
    while True:
        nexts = [
            r["to_version"]
            for r in conn.execute(
                "SELECT DISTINCT to_version FROM serving_mappings "
                "WHERE from_version = ? ORDER BY to_version",
                (cur,),
            )
        ]
        if not nexts:
            break  # 已到链尾（现行版）
        if len(nexts) > 1:  # 防御：线性供数链每版最多一个后继
            raise HTTPException(500, f"供数链出现分支: {cur} -> {nexts}")
        nxt = nexts[0]
        if nxt in seen:  # 防御：不应出现环
            raise HTTPException(500, f"供数链出现环: {cur} -> {nxt}")
        seen.add(nxt)
        links.append((cur, nxt))
        cur = nxt
    return links


def _propagate(conn: sqlite3.Connection, version: str, code: str) -> list[dict]:
    """单份鉴定沿日常供数链逐段转换，返回 [{version, codes}, ...]（含登记版起点）。"""
    chain = [{"version": version, "codes": [code]}]
    frontier = [code]
    for from_ver, to_ver in _children_chain(conn, version):
        nxt: set[str] = set()
        for c in frontier:
            rows = conn.execute(
                "SELECT to_code FROM serving_mappings "
                "WHERE from_version=? AND to_version=? AND from_code=?",
                (from_ver, to_ver, c),
            ).fetchall()
            if not rows:
                # 正常不会发生：退役时已保证合成映射完整；此处为防御
                raise HTTPException(
                    500,
                    f"供数映射链断裂: {from_ver} 代码 {c} 在 {to_ver} 无去向（数据异常）",
                )
            nxt.update(r["to_code"] for r in rows)
        frontier = sorted(nxt)
        chain.append({"version": to_ver, "codes": frontier})
    return chain


def resolve_specimen(conn: sqlite3.Connection, specimen_no: str) -> dict:
    """返回现行版号与每份有效鉴定逐版转换后的候选交集。

    整个读取在单个只读事务（快照）内完成，换版写入不影响本次查询。
    """
    conn.execute("BEGIN")  # 延迟型只读快照：首条 SELECT 时建立
    try:
        current = get_current_version(conn)
        if current is None:
            raise HTTPException(409, "尚无名录版本，请先初始化")

        id_rows = conn.execute(
            "SELECT * FROM identifications "
            "WHERE specimen_no=? AND status='active' ORDER BY id",
            (specimen_no,),
        ).fetchall()

        traces: list[dict] = []
        intersection: set[str] | None = None

        for row in id_rows:
            chain = _propagate(conn, row["version_code"], row["species_code"])
            if chain[-1]["version"] != current:
                # 防御：供数链必须抵达现行版，否则说明映射/退役数据不一致
                raise HTTPException(
                    500,
                    f"鉴定 {row['id']} 的供数链止于 {chain[-1]['version']}，"
                    f"未抵达现行版 {current}（数据异常）",
                )
            candidates = chain[-1]["codes"]
            traces.append(
                {
                    "identification_id": row["id"],
                    "registered_version": row["version_code"],
                    "registered_code": row["species_code"],
                    "chain": chain,
                    "candidates_at_current": candidates,
                }
            )
            cand_set = set(candidates)
            intersection = cand_set if intersection is None else intersection & cand_set

        if not traces:
            status = STATUS_UNIDENTIFIED
            resolved: str | None = None
            final_candidates: list[str] = []
        else:
            assert intersection is not None
            final_candidates = sorted(intersection)
            if not final_candidates:
                status = STATUS_CONFLICT
                resolved = None
            elif len(final_candidates) == 1:
                status = STATUS_UNIQUE
                resolved = final_candidates[0]
            else:
                status = STATUS_REVIEW
                resolved = None

        return {
            "specimen_no": specimen_no,
            "current_version": current,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "resolved_species_code": resolved,
            "candidates": final_candidates,
            "active_identifications": traces,
            "snapshot_consistent": True,
        }
    finally:
        # 只读事务提交（释放快照），出错也不影响数据
        conn.execute("COMMIT")
