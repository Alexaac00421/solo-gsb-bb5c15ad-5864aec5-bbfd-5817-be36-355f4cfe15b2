-- 实验室设备校准追溯服务 —— 数据库初始化脚本
-- 由 docker-compose 挂载到 postgres 容器的 /docker-entrypoint-initdb.d/ 自动执行

-- exclusion 约束需要 btree_gist（text = text 的 GiST 操作符）
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------------------
-- 证书表
--   * 有效期区间左闭右开 [valid_from, valid_to)，起点必须早于终点
--   * 同一设备任意两份证书的有效期不得重叠（exclusion 约束，并发下只有一份成功）
--   * 幂等键全局唯一，配合 request_hash 识别"同键异内容"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS certificates (
    id              BIGSERIAL    PRIMARY KEY,
    idempotency_key TEXT         NOT NULL,
    request_hash    TEXT         NOT NULL,
    device_id       TEXT         NOT NULL,
    certificate_no  TEXT         NOT NULL,
    valid_from      TIMESTAMPTZ  NOT NULL,
    valid_to        TIMESTAMPTZ  NOT NULL,
    revoked_at      TIMESTAMPTZ,
    revoke_reason   TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT certificates_valid_range_chk CHECK (valid_from < valid_to),
    CONSTRAINT certificates_idempotency_key_uq UNIQUE (idempotency_key),
    CONSTRAINT certificates_device_no_uq UNIQUE (device_id, certificate_no),
    CONSTRAINT certificates_no_overlap_excl EXCLUDE USING gist (
        device_id WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    )
);

CREATE INDEX IF NOT EXISTS certificates_device_idx ON certificates (device_id);

-- ---------------------------------------------------------------------------
-- 测量批次表：批次键幂等 + 首次结果持久化
--   * batch_key 全局唯一：并发提交同一键至多生成一批（唯一约束保证）
--   * request_hash 为有序记录内容的规范化哈希（测量时刻统一换算 UTC），
--     用于识别"同键不同内容"冲突；记录顺序不同也算不同内容
--   * 首次提交的最终结果原样持久化：
--       succeeded -> measurement_ids（与输入顺序一一对应）
--       failed    -> 第一条不合格记录的序号（0-based）与原因
--     此后同键同内容重试（即使证书已撤销）一律返回首次结果
--   * 批次行只追加，不提供修改/删除接口
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurement_batches (
    id              BIGSERIAL    PRIMARY KEY,
    batch_key       TEXT         NOT NULL,
    request_hash    TEXT         NOT NULL,
    status          TEXT         NOT NULL,
    measurement_ids BIGINT[],
    error_index     INT,
    error_code      TEXT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT measurement_batches_key_uq UNIQUE (batch_key),
    CONSTRAINT measurement_batches_status_chk CHECK (status IN ('succeeded', 'failed')),
    CONSTRAINT measurement_batches_result_chk CHECK (
        (status = 'succeeded' AND measurement_ids IS NOT NULL
                               AND error_index IS NULL AND error_code IS NULL)
        OR
        (status = 'failed' AND measurement_ids IS NULL
                            AND error_index IS NOT NULL AND error_code IS NOT NULL
                            AND error_message IS NOT NULL)
    )
);

-- ---------------------------------------------------------------------------
-- 测量记录表：一经写入不可修改、不可删除（触发器强制）
--   * batch_id 非空时标识该记录由哪一批次写入（单条写入为 NULL）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurements (
    id             BIGSERIAL    PRIMARY KEY,
    batch_id       BIGINT       REFERENCES measurement_batches (id) DEFERRABLE INITIALLY DEFERRED,
    device_id      TEXT         NOT NULL,
    measured_at    TIMESTAMPTZ  NOT NULL,
    certificate_id BIGINT       NOT NULL REFERENCES certificates (id),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS measurements_device_idx ON measurements (device_id);
CREATE INDEX IF NOT EXISTS measurements_cert_idx   ON measurements (certificate_id);
CREATE INDEX IF NOT EXISTS measurements_batch_idx  ON measurements (batch_id);

-- 兼容旧库：measurements 已存在时幂等补列（外键同样设为延迟约束）
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS batch_id BIGINT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'measurements_batch_id_fkey'
    ) THEN
        ALTER TABLE measurements
            ADD CONSTRAINT measurements_batch_id_fkey
            FOREIGN KEY (batch_id) REFERENCES measurement_batches (id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS measurements_batch_idx ON measurements (batch_id);

CREATE OR REPLACE FUNCTION reject_measurement_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'measurements are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS measurements_immutable_trg ON measurements;
CREATE TRIGGER measurements_immutable_trg
    BEFORE UPDATE OR DELETE ON measurements
    FOR EACH ROW EXECUTE FUNCTION reject_measurement_mutation();

-- ---------------------------------------------------------------------------
-- 测量更正表：追加式更正链
--   * 原始登记为 version 1，每次更正追加一行，version 单调 +1（CHECK >= 2）
--   * (measurement_id, version) 唯一：同一版本的并发更正最多成功一次
--   * 只增不改：与测量表一样挂不可变触发器，任何版本不得修改或删除
--   * 设备编号不在本表：更正不改变设备，查询时从原始测量记录取得
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurement_corrections (
    id              BIGSERIAL    PRIMARY KEY,
    measurement_id  BIGINT       NOT NULL REFERENCES measurements (id),
    version         INT          NOT NULL,
    measured_at     TIMESTAMPTZ  NOT NULL,
    certificate_id  BIGINT       NOT NULL REFERENCES certificates (id),
    reason          TEXT         NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT measurement_corrections_version_chk CHECK (version >= 2),
    CONSTRAINT measurement_corrections_version_uq UNIQUE (measurement_id, version)
);

CREATE INDEX IF NOT EXISTS measurement_corrections_measurement_idx
    ON measurement_corrections (measurement_id);

CREATE OR REPLACE FUNCTION reject_correction_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'measurement corrections are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS measurement_corrections_immutable_trg ON measurement_corrections;
CREATE TRIGGER measurement_corrections_immutable_trg
    BEFORE UPDATE OR DELETE ON measurement_corrections
    FOR EACH ROW EXECUTE FUNCTION reject_correction_mutation();

-- 批次行同样只追加：首次结果一经确定不得修改或删除（保证幂等重放永远返回首次结果）
CREATE OR REPLACE FUNCTION reject_batch_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'measurement batches are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS measurement_batches_immutable_trg ON measurement_batches;
CREATE TRIGGER measurement_batches_immutable_trg
    BEFORE UPDATE OR DELETE ON measurement_batches
    FOR EACH ROW EXECUTE FUNCTION reject_batch_mutation();

-- ---------------------------------------------------------------------------
-- 审计快照表：按设备编号 + 带时区时间窗（左闭右开）冻结的合规复核快照
--   * 生成时在 REPEATABLE READ 单事务内读取"测量最新版本 + 证书撤销状态"
--     的同一一致视图，并发更正/撤销只能整体位于快照之前或之后
--   * 计数（valid_count / invalid_count）与明细在同一事务原子写入
--   * 快照行只追加：生成后更正、撤销或新写入均不得改变既有快照（触发器强制）
--   * 时间窗为空窗（无命中记录）也正常生成，两个计数均为 0
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_snapshots (
    id            BIGSERIAL    PRIMARY KEY,
    device_id     TEXT         NOT NULL,
    window_start  TIMESTAMPTZ  NOT NULL,
    window_end    TIMESTAMPTZ  NOT NULL,
    valid_count   INT          NOT NULL,
    invalid_count INT          NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT audit_snapshots_window_chk CHECK (window_start < window_end),
    CONSTRAINT audit_snapshots_counts_chk CHECK (valid_count >= 0 AND invalid_count >= 0)
);

CREATE INDEX IF NOT EXISTS audit_snapshots_device_idx ON audit_snapshots (device_id);

-- ---------------------------------------------------------------------------
-- 审计快照明细表：快照生成时刻每条命中测量的冻结最新版本
--   * 仅收录"最新版本 measured_at 落在 [window_start, window_end) 内"的测量；
--     单条写入与批次写入的测量一视同仁
--   * 证书编号、有效性及原因按快照生成时的一致视图冻结，
--     此后证书撤销 / 测量更正 / 新写入均不改变本表（触发器强制，且无外键关联实时表）
--   * (snapshot_id, measurement_id) 为主键：分页按测量编号稳定游标读取
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_snapshot_items (
    snapshot_id     BIGINT       NOT NULL REFERENCES audit_snapshots (id)
                    DEFERRABLE INITIALLY DEFERRED,
    measurement_id  BIGINT       NOT NULL,
    version         INT          NOT NULL,
    measured_at     TIMESTAMPTZ  NOT NULL,
    certificate_no  TEXT         NOT NULL,
    is_valid        BOOLEAN      NOT NULL,
    reason          TEXT         NOT NULL,

    CONSTRAINT audit_snapshot_items_pk PRIMARY KEY (snapshot_id, measurement_id)
);

CREATE INDEX IF NOT EXISTS audit_snapshot_items_snapshot_idx
    ON audit_snapshot_items (snapshot_id, measurement_id);

-- 快照及其明细同样只追加：明细与计数一经冻结不得修改或删除
CREATE OR REPLACE FUNCTION reject_audit_snapshot_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit snapshots and their items are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_snapshots_immutable_trg ON audit_snapshots;
CREATE TRIGGER audit_snapshots_immutable_trg
    BEFORE UPDATE OR DELETE ON audit_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_audit_snapshot_mutation();

DROP TRIGGER IF EXISTS audit_snapshot_items_immutable_trg ON audit_snapshot_items;
CREATE TRIGGER audit_snapshot_items_immutable_trg
    BEFORE UPDATE OR DELETE ON audit_snapshot_items
    FOR EACH ROW EXECUTE FUNCTION reject_audit_snapshot_mutation();
