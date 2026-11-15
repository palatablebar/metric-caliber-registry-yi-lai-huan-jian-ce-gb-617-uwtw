-- 环处置记录表结构参考（PostgreSQL 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）
--
-- 每个基本环一条台账：fingerprint（环上指标 + 环内引用边）唯一建档；
-- 内容版本与处置事件只追加不覆盖；同一快照重跑不重复追加。

CREATE TABLE cycle_records (
    id                  SERIAL PRIMARY KEY,
    record_no           VARCHAR(40)  NOT NULL UNIQUE,   -- CYC-0001
    fingerprint         VARCHAR(64)  NOT NULL UNIQUE,   -- 环上指标+引用边的 sha256，识别同一问题
    status              VARCHAR(16)  NOT NULL,          -- open/resolved
    members             JSON,                           -- 环上指标编码（当前确认值）
    edges               JSON,                           -- 环内引用边 [{from,to}]
    path                JSON,                           -- 代表环路径
    component           JSON,                           -- 所属强连通区域
    length              INTEGER      NOT NULL DEFAULT 0,
    severity            VARCHAR(12)  NOT NULL,
    score               INTEGER      NOT NULL DEFAULT 0,
    impact              JSON,                           -- 当时的下游影响面快照
    summary             VARCHAR(300) NOT NULL DEFAULT '',
    suggestion          TEXT         NOT NULL DEFAULT '',
    first_seen_run_id   INTEGER      REFERENCES runs(id),  -- 首次发现
    first_seen_at       TIMESTAMP,
    last_seen_run_id    INTEGER      REFERENCES runs(id),  -- 最后确认
    last_seen_at        TIMESTAMP,
    resolved_run_id     INTEGER      REFERENCES runs(id),  -- 闭环
    resolved_at         TIMESTAMP,
    resolved_by         VARCHAR(160),
    resolution          JSON,                           -- 闭环结果 {broken_edges, note}
    revision_count      INTEGER      NOT NULL DEFAULT 1,
    created_at          TIMESTAMP    NOT NULL,
    updated_at          TIMESTAMP    NOT NULL
);
CREATE INDEX ix_cycle_records_status ON cycle_records (status);

CREATE TABLE cycle_record_revisions (
    id          SERIAL PRIMARY KEY,
    record_id   INTEGER      NOT NULL REFERENCES cycle_records(id) ON DELETE CASCADE,
    revision    INTEGER      NOT NULL,                -- 口径/影响面每次变化 +1，旧版本永不覆盖
    run_id      INTEGER      REFERENCES runs(id),
    members     JSON,
    edges       JSON,
    path        JSON,
    severity    VARCHAR(12)  NOT NULL,
    score       INTEGER      NOT NULL DEFAULT 0,
    impact      JSON,
    note        VARCHAR(120) NOT NULL DEFAULT '',
    created_at  TIMESTAMP    NOT NULL,
    CONSTRAINT uq_cycle_revision UNIQUE (record_id, revision)
);

CREATE TABLE cycle_record_events (
    id          SERIAL PRIMARY KEY,
    record_id   INTEGER      NOT NULL REFERENCES cycle_records(id) ON DELETE CASCADE,
    run_id      INTEGER      REFERENCES runs(id),
    event       VARCHAR(16)  NOT NULL,                -- detected/confirmed/reopened/resolved
    actor_email VARCHAR(160) NOT NULL DEFAULT 'system',
    detail      JSON,                                 -- resolved 时含 broken_edges
    created_at  TIMESTAMP    NOT NULL,
    CONSTRAINT uq_cycle_event UNIQUE (record_id, run_id, event)  -- 同一快照重跑不重复追加
);
