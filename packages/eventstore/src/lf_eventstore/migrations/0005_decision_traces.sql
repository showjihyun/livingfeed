-- 결정 트레이스 — 프롬프트 원문의 집 (ADR-021 §2/§5).
--
-- 왜 이벤트가 아니라 별도 표인가: 번들 예산이 입력 6K 토큰(≈24KB)이라, ADR-020의
-- 호출량(~17K/day)에 곱하면 트레이스는 이벤트 저장(~2GB/월)의 약 6배(~12.6GB/월)다.
-- 이벤트 로그에 실으면 ADR-002의 파티셔닝·ADR-005의 SoT 운영 전제가 무너진다.
--
-- 더 근본적인 이유는 수명이다: 이벤트는 영구 보존이고 트레이스는 소모품이다.
-- 같은 통에 담으면 짧은 쪽에 맞춰 역사를 버리거나, 긴 쪽에 맞춰 프롬프트를
-- 영구 보관하게 된다. 둘 다 틀렸다.
--
-- actor.decision.made 이벤트(섹션 구조·digest)는 여기 없어도 항상 남는다 —
-- 그것이 L1 재조립 보증의 근거이므로 샘플링 대상이 아니다 (ADR-021 §5).
CREATE TABLE IF NOT EXISTS es.decision_traces (
    trace_id      TEXT PRIMARY KEY,          -- actor.decision.made.payload.trace_id
    world_id      TEXT NOT NULL,
    actor_id      TEXT,                      -- 세계 단위 결정(Director)은 주체가 없다
    tick          BIGINT NOT NULL,
    purpose       TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    user_prompt   TEXT NOT NULL,
    output        TEXT,                      -- 응답 원문 (실패면 null)
    model         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL       -- 보존 기한 — 모드가 정한다 (기본 7일/연구 30일)
);

-- 정리 배치의 경로 — 기한 지난 것만 지운다. 트레이스는 소모품이라 정리가
-- 상시 동작이며, 인덱스 없이는 12GB/월 규모에서 순차 스캔이 된다.
CREATE INDEX IF NOT EXISTS decision_traces_expiry ON es.decision_traces (expires_at);

-- 연구 질의의 축 — "이 세계 이 구간의 결정들을 다시 읽자"
CREATE INDEX IF NOT EXISTS decision_traces_by_world_tick
    ON es.decision_traces (world_id, tick);
