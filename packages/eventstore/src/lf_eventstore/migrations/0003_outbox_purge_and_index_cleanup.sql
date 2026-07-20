-- 인덱스 정리 (⑤ 무제한 조회 방어 증분과 함께) — 쓰기 증폭 제거 + 정리 경로 가속.
--
-- ① events_by_correlation(correlation_id 단독) 제거: 0002의
--    events_by_world_correlation(world_id, correlation_id)가 실제 질의 모양을
--    덮는다 (feed-api /story: WHERE world_id = %s AND correlation_id = %s — 질의는
--    항상 world_id를 동반한다). 단독 인덱스를 쓰는 질의는 없어, 남겨두면 매 append의
--    순수 쓰기 증폭일 뿐이다 (0002 주석의 예고를 여기서 집행한다).
DROP INDEX IF EXISTS es.events_by_correlation;

-- ② outbox 정리 인덱스: purge_published는 published_at IS NOT NULL 이면서 기한이
--    지난 행을 DELETE 한다. 기존 outbox_unpublished는 published_at IS NULL 부분
--    인덱스라 정리 질의(발행 완료분)엔 무용 — 발행된 행만 담는 부분 인덱스를 둬
--    보존 기간이 긴 outbox에서도 정리가 인덱스 스캔으로 끝나게 한다 (ADR-005).
CREATE INDEX IF NOT EXISTS outbox_published_at ON es.outbox (published_at)
    WHERE published_at IS NOT NULL;
