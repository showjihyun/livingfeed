-- 이벤트 스토어 스키마 (ADR-005 §이벤트 스토어 스키마)
-- es 스키마는 initdb(01-schemas.sql)에서도 만들지만, 맨몸 DB(CI 서비스 컨테이너)에서도
-- 마이그레이션만으로 완결되도록 여기서도 보장한다.
CREATE SCHEMA IF NOT EXISTS es;

CREATE TABLE es.events (
    global_seq      BIGINT GENERATED ALWAYS AS IDENTITY,
    event_id        TEXT NOT NULL,
    world_id        TEXT NOT NULL,
    stream          TEXT NOT NULL,
    stream_key      TEXT NOT NULL,
    stream_seq      BIGINT NOT NULL,
    type            TEXT NOT NULL,
    schema_version  SMALLINT NOT NULL DEFAULT 1,
    actor_id        TEXT,                        -- 봉투 필수 필드 (ADR-002) — null 허용
    tick            BIGINT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    causation_id    TEXT,
    correlation_id  TEXT,
    payload         JSONB NOT NULL,
    -- PG 제약: 파티션 테이블의 PK는 파티션 키(tick)를 반드시 포함해야 한다
    PRIMARY KEY (world_id, stream, stream_key, stream_seq, tick)
) PARTITION BY RANGE (tick);

-- 파티션: 43,200 tick = 실시간 30일 (1 tick = 60s, ADR-011). 초기 1년치 + DEFAULT.
-- 이후 파티션 관리(생성/DETACH/아카이브)는 운영 작업이다 (ADR-005 §파티셔닝).
DO $$
DECLARE i int;
BEGIN
    FOR i IN 0..11 LOOP
        EXECUTE format(
            'CREATE TABLE es.events_p%s PARTITION OF es.events FOR VALUES FROM (%s) TO (%s)',
            lpad((i * 43200)::text, 7, '0'), i * 43200, (i + 1) * 43200
        );
    END LOOP;
END $$;
CREATE TABLE es.events_default PARTITION OF es.events DEFAULT;

-- event_id 전역 유니크는 파티션 제약상 인덱스로 강제할 수 없다 — 조회용 (ADR-005)
CREATE INDEX events_by_event_id ON es.events (event_id);
CREATE INDEX events_by_tick ON es.events (world_id, tick);
CREATE INDEX events_by_correlation ON es.events (correlation_id);

-- 스트림 순번(낙관적 동시성)의 전역 유일성 집행 지점 (ADR-005 §동시성 제어)
CREATE TABLE es.stream_heads (
    world_id    TEXT NOT NULL,
    stream      TEXT NOT NULL,
    stream_key  TEXT NOT NULL,
    head_seq    BIGINT NOT NULL,
    PRIMARY KEY (world_id, stream, stream_key)
);

-- Transactional Outbox (ADR-005 §Transactional Outbox, ADR-017 §1)
-- subject는 저장하지 않는다 — 발행 시점에 dispatcher가 계산한다 (ADR-017 §3).
CREATE TABLE es.outbox (
    global_seq    BIGINT PRIMARY KEY,          -- es.events.global_seq
    event_id      TEXT NOT NULL,
    envelope      JSONB NOT NULL,              -- 발행할 완전한 봉투
    enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ                  -- NULL = 미발행
);
CREATE INDEX outbox_unpublished ON es.outbox (global_seq) WHERE published_at IS NULL;

-- 액터 스냅샷 — 500 이벤트마다, 언제든 삭제·재생성 가능한 캐시 (ADR-002 규칙 4)
CREATE TABLE es.actor_snapshots (
    world_id    TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    stream_seq  BIGINT NOT NULL,
    tick        BIGINT NOT NULL,
    state       JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, actor_id, stream_seq)
);
