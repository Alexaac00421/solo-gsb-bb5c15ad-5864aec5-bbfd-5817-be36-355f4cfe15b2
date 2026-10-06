"""测量有效性判定：按证书当前撤销状态生成有效性与原因。

实时查询与审计快照冻结明细共用同一原因文案，保证两种入口的口径一致。
"""

from .schemas import Validity

# 有效记录的固定原因文案：实时查询返回它，审计快照生成时冻结它
VALID_REASON = "certificate is active and covers the measurement time"


def validity_for_certificate(revoked_at, revoke_reason) -> Validity:
    """根据证书撤销状态构造有效性：撤销即无效并给出撤销时刻与原因。"""
    if revoked_at is not None:
        return Validity(
            is_valid=False,
            reason=revoke_reason_text(revoked_at, revoke_reason),
        )
    return Validity(is_valid=True, reason=VALID_REASON)


def revoke_reason_text(revoked_at, revoke_reason) -> str:
    """审计快照明细冻结用：返回与实时查询一致的撤销原因文案。"""
    reason = f"certificate revoked at {revoked_at.isoformat()}"
    if revoke_reason:
        reason += f": {revoke_reason}"
    return reason
