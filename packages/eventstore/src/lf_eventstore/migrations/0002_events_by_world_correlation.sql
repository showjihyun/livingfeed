-- 서사 사슬 조회 인덱스 — "이야기의 시작점" (plan/03 §단계 3→4, ADR-002 규칙 5)
-- feed-api GET /story/{correlation_id}가 한 correlation의 이벤트들을 world 안에서
-- global_seq 순으로 읽는다. 기존 events_by_correlation(correlation_id 단독)은
-- world 필터가 없어 이 질의 모양과 어긋난다 — 질의는 항상 world_id를 동반한다.
CREATE INDEX events_by_world_correlation ON es.events (world_id, correlation_id);
