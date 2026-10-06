"""审计快照：按设备编号 + 带时区时间窗生成不可变的合规复核快照，并稳定分页读取明细。

一致性模型：

- 生成在单个 REPEATABLE READ 事务内完成：测量最新版本（原始登记或最近一次更正）
  与证书撤销状态取自同一快照视图，并发更正/撤销只能整体位于快照之前或之后，
  不会出现"版本来自撤销前、撤销状态来自撤销后"之类的撕裂读取；
- 命中条件只看**最新版本**的 measured_at 是否落在左闭右开窗口
  [window_start, window_end)；单条写入与批次写入的测量一视同仁；
- 计数与明细在同一事务原子冻结写入 audit_snapshot_items（不保留对实时表的引用），
  此后更正、撤销或新写入均不改变已生成的明细和总数（两表均挂不可变触发器）；
- 明细按测量编号（measurement_id）稳定排序，分页用 measurement_id 游标翻页，
  不受快照之后新写入/更正的影响（读的本来就是冻结副本）。
"""

import asyncpg
from fastapi import APIRouter, Depends, Query

from ..db import get_pool
from ..errors import ApiError
from ..schemas import (
    AuditSnapshotCreate,
    AuditSnapshotItem,
    AuditSnapshotOut,
    AuditSnapshotPage,
    Validity,
)
from ..validity import VALID_REASON, revoke_reason_text

router = APIRouter()

# 某设备每条测量的"最新版本"行：有更正取最新更正、无更正取原始登记。
# 在外层 REPEATABLE READ 事务中，本查询与 certificates 的 JOIN
# 以及随后的写入共享同一快照，版本与撤销状态取自一致视图。
_LATEST_VERSION_SQL = """
SELECT latest.measurement_id,
       latest.version,
       latest.measured_at,
       latest.certificate_id,
       c.certificate_no,
       c.revoked_at,
       c.revoke_reason
FROM (
    SELECT m.id AS measurement_id,
           1  AS version,
           m.measured_at,
           m.certificate_id
    FROM measurements m
    WHERE m.device_id = $1
      AND NOT EXISTS (
          SELECT 1 FROM measurement_corrections mc
          WHERE mc.measurement_id = m.id
      )

    UNION ALL

    SELECT measurement_id, version, measured_at, certificate_id
    FROM (
        -- 有更正的测量：只取版本号最大的一行（分支各自成子查询，
        -- 使 DISTINCT ON 的 ORDER BY 不被解析为整个 UNION 的排序）
        SELECT DISTINCT ON (mc.measurement_id)
               mc.measurement_id,
               mc.version,
               mc.measured_at,
               mc.certificate_id
        FROM measurement_corrections mc
        JOIN measurements mm ON mm.id = mc.measurement_id
        WHERE mm.device_id = $1
        ORDER BY mc.measurement_id, mc.version DESC
    ) corrected
) AS latest
JOIN certificates c ON c.id = latest.certificate_id
WHERE latest.measured_at >= $2
  AND latest.measured_at <  $3
ORDER BY latest.measurement_id
"""


def _snapshot_not_found(snapshot_id: int) -> ApiError:
    return ApiError(404, "SNAPSHOT_NOT_FOUND", f"audit snapshot id={snapshot_id} not found")


@router.post("", response_model=AuditSnapshotOut, status_code=201)
async def create_audit_snapshot(
    payload: AuditSnapshotCreate,
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotOut:
    """生成审计快照。

    - 时间窗左闭右开 [window_start, window_end)，两端时刻均须带时区、起点早于终点；
      非法请求 422 拒绝且不留任何快照；
    - 版本与证书撤销状态取自同一一致视图（REPEATABLE READ 单事务），
      并发更正/撤销整体位于快照之前或之后；
    - 空窗（窗口内无最新版本命中记录）照常生成快照，valid/invalid 计数均为 0。
    """
    async with pool.acquire() as conn:
        # REPEATABLE READ：事务内所有读共享同一快照，避免版本视图与撤销状态撕裂；
        # 不锁测量/更正/证书行，故与正常的实时写入、更正、撤销互不阻塞——
        # 并发事务要么已提交（被本快照看到），要么未提交/后提交（整体落在快照之后）。
        async with conn.transaction(isolation="repeatable_read"):
            rows = await conn.fetch(
                _LATEST_VERSION_SQL,
                payload.device_id,
                payload.window_start,
                payload.window_end,
            )

            valid_count = sum(1 for r in rows if r["revoked_at"] is None)
            invalid_count = len(rows) - valid_count

            header = await conn.fetchrow(
                "INSERT INTO audit_snapshots "
                "(device_id, window_start, window_end, valid_count, invalid_count) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                payload.device_id,
                payload.window_start,
                payload.window_end,
                valid_count,
                invalid_count,
            )

            if rows:
                await conn.executemany(
                    "INSERT INTO audit_snapshot_items "
                    "(snapshot_id, measurement_id, version, measured_at, "
                    " certificate_no, is_valid, reason) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    [
                        (
                            header["id"],
                            r["measurement_id"],
                            r["version"],
                            r["measured_at"],
                            r["certificate_no"],
                            r["revoked_at"] is None,
                            revoke_reason_text(r["revoked_at"], r["revoke_reason"])
                            if r["revoked_at"] is not None
                            else VALID_REASON,
                        )
                        for r in rows
                    ],
                )

    return AuditSnapshotOut(
        snapshot_id=header["id"],
        device_id=header["device_id"],
        window_start=header["window_start"],
        window_end=header["window_end"],
        valid_count=header["valid_count"],
        invalid_count=header["invalid_count"],
        total_count=header["valid_count"] + header["invalid_count"],
        created_at=header["created_at"],
    )


@router.get("/{snapshot_id}", response_model=AuditSnapshotOut)
async def get_audit_snapshot(
    snapshot_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotOut:
    """读取快照头：快照标识、时间窗与有效/无效总数；未知标识返回 404。"""
    row = await pool.fetchrow("SELECT * FROM audit_snapshots WHERE id = $1", snapshot_id)
    if row is None:
        raise _snapshot_not_found(snapshot_id)
    return AuditSnapshotOut(
        snapshot_id=row["id"],
        device_id=row["device_id"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        valid_count=row["valid_count"],
        invalid_count=row["invalid_count"],
        total_count=row["valid_count"] + row["invalid_count"],
        created_at=row["created_at"],
    )


@router.get("/{snapshot_id}/items", response_model=AuditSnapshotPage)
async def list_audit_snapshot_items(
    snapshot_id: int,
    after_measurement_id: int | None = Query(
        default=None,
        ge=1,
        description="测量编号游标：只返回 measurement_id 严格大于该值的明细；"
        "首页不传，翻页取上一页最后一条的 measurement_id",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="每页条数（1–1000）"),
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotPage:
    """按测量编号稳定分页读取快照明细（左闭右开游标，固定字段输出）。

    明细在生成时已冻结，分页只与 measurement_id 游标有关，
    不受快照之后的新写入、更正或撤销影响。
    """
    header = await pool.fetchrow("SELECT * FROM audit_snapshots WHERE id = $1", snapshot_id)
    if header is None:
        raise _snapshot_not_found(snapshot_id)

    # 多取一条判断是否还有下一页；主键 (snapshot_id, measurement_id) 直接支撑该范围扫描
    rows = await pool.fetch(
        "SELECT measurement_id, version, measured_at, certificate_no, is_valid, reason "
        "FROM audit_snapshot_items "
        "WHERE snapshot_id = $1 "
        "  AND ($2::bigint IS NULL OR measurement_id > $2) "
        "ORDER BY measurement_id LIMIT $3",
        snapshot_id,
        after_measurement_id,
        limit + 1,
    )
    has_next = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        AuditSnapshotItem(
            measurement_id=r["measurement_id"],
            version=r["version"],
            measured_at=r["measured_at"],
            certificate_no=r["certificate_no"],
            validity=Validity(is_valid=r["is_valid"], reason=r["reason"]),
        )
        for r in page_rows
    ]
    return AuditSnapshotPage(
        snapshot_id=header["id"],
        device_id=header["device_id"],
        window_start=header["window_start"],
        window_end=header["window_end"],
        limit=limit,
        next_after_measurement_id=page_rows[-1]["measurement_id"]
        if has_next and page_rows
        else None,
        items=items,
    )
