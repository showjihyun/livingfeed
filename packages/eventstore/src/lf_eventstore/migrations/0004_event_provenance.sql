-- 출처 등급(provenance)을 이벤트의 1급 필드로 (ADR-021 §1).
--
-- "이 인물이 방금 한 말은 기억한 것인가, 방금 지어낸 것인가" — 이 구분이 데이터에
-- 없으면 세계에서 관찰되는 창발은 검증 불가능한 주장이 된다. 봉투 필수 필드이며
-- 쓰기 경로(store._validate)가 근거 없는 출처 주장을 거부한다.
ALTER TABLE es.events ADD COLUMN provenance JSONB;

-- 기존 적재분의 백필 — 'unknown'이다.
--
-- 여기서 derived나 generated로 추정해 채우면 안 된다. 그 추정이 틀렸을 때
-- 데이터는 "출처를 모른다"가 아니라 "출처가 이것이다"라고 거짓말하게 되고,
-- 그건 출처 필드가 아예 없는 것보다 나쁘다 — ADR-021이 막으려는 바로 그 오염이다.
-- unknown은 읽기에서만 유효한 값이며, 새 이벤트에는 쓸 수 없다.
UPDATE es.events SET provenance = '{"kind": "unknown"}'::jsonb WHERE provenance IS NULL;

ALTER TABLE es.events ALTER COLUMN provenance SET NOT NULL;

-- outbox의 봉투(JSONB)에도 같은 백필 — relay가 발행하는 봉투는 이 컬럼이 원천이라
-- 미발행분이 남아 있으면 provenance 없는 봉투가 JetStream으로 나간다.
UPDATE es.outbox
SET envelope = jsonb_set(envelope, '{provenance}', '{"kind": "unknown"}'::jsonb)
WHERE NOT envelope ? 'provenance';

-- 등급별 조회 인덱스 — 연구 질의의 기본 축이다 ("이 세계의 생성물만 걸러 보기").
-- 표현식 인덱스로 kind만 색인한다: 근거 필드(trace_id 등)까지 담는 전체 jsonb_path_ops는
-- 쓰기 증폭이 크고, 실제 질의 모양은 world_id + kind다 (ADR-021 §1, 0003의 결과).
CREATE INDEX events_by_provenance_kind
    ON es.events (world_id, (provenance ->> 'kind'));
