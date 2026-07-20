"""outbox relay — 발행의 유일한 경로 (ADR-017 §1, ADR-005 §Transactional Outbox).

단일 활성 인스턴스(PG advisory lock 리더 선출) + 대기 standby.
LISTEN 웨이크업 + 폴링 폴백으로 outbox를 global_seq 순으로 JetStream에 발행한다.
발행은 멱등이다 — Nats-Msg-Id(event_id)를 JetStream dedup window가 흡수한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator
from lf_eventstore import OUTBOX_CHANNEL, fetch_unpublished, mark_published, purge_published
from lf_schemas import registry
from psycopg import AsyncConnection

from lf_dispatcher.config import Config
from lf_dispatcher.streams import ensure_streams
from lf_dispatcher.subjects import dlq_subject, subject

logger = logging.getLogger("lf.dispatcher.relay")

#: relay 리더 선출용 advisory lock 키 — 세션 락 (연결이 살아있는 동안 유지)
RELAY_LOCK_KEY = 7_420_250_717


class EnvelopeGateError(Exception):
    """relay 방어선의 봉투 검증 실패 — DLQ 대상 (ADR-017 §2)."""


# 봉투 검증기는 한 번만 컴파일한다 — relay는 발행 행마다 _gate를 도는 핫패스라,
# append마다 새 Draft202012Validator를 만들면 메타스키마·$ref 처리가 매번 낭비된다.
# 스키마 원천(lf_schemas.registry)이 @cache라 프로세스 수명 내 불변이다.
@cache
def _envelope_validator() -> Draft202012Validator:
    return Draft202012Validator(registry.envelope_schema())


def _gate(env: str, envelope: dict[str, Any]) -> str:
    """봉투를 재검증하고 발행 subject를 반환한다.

    적재 시점 검증(lf-eventstore)이 1차 방어선이고, 여기는 구버전 잔재 등을
    거르는 2차 방어선이다. 실패는 EnvelopeGateError — 호출자가 DLQ로 보낸다.
    """
    errors = [
        f"{'/'.join(map(str, err.absolute_path)) or '(root)'}: {err.message}"
        for err in _envelope_validator().iter_errors(envelope)
    ]
    if errors:
        raise EnvelopeGateError("; ".join(errors))
    try:
        return subject(env, envelope["world_id"], envelope["stream"], envelope["type"])
    except (ValueError, KeyError):  # subject 규칙 위반도 게이트 실패다 (ADR-017 §3)
        raise EnvelopeGateError(f"subject 구성 불가: {envelope.get('type')}") from None


async def relay_once(conn: AsyncConnection, js: Any, env: str, *, batch_size: int = 500) -> int:
    """미발행 outbox를 한 번 비운다. 반환: 발행(또는 DLQ 이동) 후 마킹한 행 수.

    NATS 발행 실패는 예외로 전파된다 — 마킹되지 않은 행은 다음 사이클에
    재발행되고 dedup window가 중복을 흡수한다 (at-least-once, ADR-005).
    """
    rows = await fetch_unpublished(conn, limit=batch_size)
    if not rows:
        return 0

    done: list[int] = []
    for row in rows:
        envelope = row.envelope
        payload = json.dumps(envelope, ensure_ascii=False).encode()
        try:
            subj = _gate(env, envelope)
        except EnvelopeGateError as e:
            # 조용한 유실은 존재하지 않는다 — DLQ로 재발행하고 알림 대상 로그 (ADR-017 §4)
            dlq = _dlq_subject_for(env, envelope)
            await js.publish(dlq, payload, headers={"Nats-Msg-Id": f"dlq-{row.event_id}"})
            logger.warning(
                "봉투 게이트 실패 → DLQ: event_id=%s subject=%s 사유=%s",
                row.event_id, dlq, e,
            )
        else:
            # 발행 실패는 전파 — 마킹 안 된 행은 다음 사이클에 재발행된다
            await js.publish(subj, payload, headers={"Nats-Msg-Id": row.event_id})
        done.append(row.global_seq)

    await mark_published(conn, done)
    return len(done)


def _dlq_subject_for(env: str, envelope: dict[str, Any]) -> str:
    try:
        original = subject(env, envelope["world_id"], envelope["stream"], envelope["type"])
        return dlq_subject(env, original)
    except Exception:
        return f"lf-dlq.{env}.invalid"


async def try_acquire_leadership(conn: AsyncConnection) -> bool:
    """relay 리더 세션 락 획득 시도 — 연결이 닫히면 자동 해제된다 (ADR-017 §1)."""
    cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (RELAY_LOCK_KEY,))
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def run_relay(cfg: Config, *, stop: asyncio.Event | None = None) -> None:
    """relay 메인 루프. stop 이벤트가 셋되면 정상 종료한다."""
    import nats
    from lf_eventstore.migrate import migrate

    stop = stop or asyncio.Event()

    async with await AsyncConnection.connect(cfg.pg_dsn, autocommit=True) as conn:
        # dev/CI 편의: 스키마를 항상 최신으로 (멱등, advisory lock 직렬화)
        applied = await migrate(conn)
        for name in applied:
            logger.info("마이그레이션 적용: %s", name)

        while not await try_acquire_leadership(conn):
            logger.info("standby — 활성 relay가 있다 (%.0fs 후 재시도)", cfg.standby_retry_s)
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.standby_retry_s)
                return
            except TimeoutError:
                continue

        logger.info("relay 리더 획득 — env=%s nats=%s", cfg.env, cfg.nats_url)
        await conn.execute(f"LISTEN {OUTBOX_CHANNEL}")

        nc = await nats.connect(cfg.nats_url)
        try:
            js = nc.jetstream()
            await ensure_streams(js)

            last_purge = 0.0
            loop = asyncio.get_running_loop()
            while not stop.is_set():
                published = await relay_once(conn, js, cfg.env, batch_size=cfg.batch_size)
                if published:
                    logger.info("발행 %d건", published)
                    continue  # 백로그가 있을 수 있다 — 즉시 다음 배치

                now = loop.time()
                if now - last_purge >= cfg.purge_interval_s:
                    purged = await purge_published(conn, keep=cfg.purge_keep)
                    if purged:
                        logger.info("outbox 정리 %d건", purged)
                    last_purge = now

                # NOTIFY 대기 — poll_interval이 지나면 폴링 폴백으로 돌아온다
                async for _ in conn.notifies(timeout=cfg.poll_interval_s, stop_after=1):
                    pass
        finally:
            await nc.drain()
