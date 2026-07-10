-- 논리 스키마 분리 (ADR-005 §역할 경계)
-- es   : 이벤트 스토어 + outbox + 스냅샷
-- read : PG 읽기 프로젝션 (프로필, 목록 등)
-- 물리 분리(Phase 2)를 대비해 둘 사이 커플링(FK, 조인 뷰)을 금지한다.
CREATE SCHEMA IF NOT EXISTS es;
CREATE SCHEMA IF NOT EXISTS read;
