"""WS /session 통합 — 커맨드 적재·ack·오류·응답 push (ADR-010 §WS).

PG+NATS 필요 (없으면 skip — conftest 참고). ASGI 테스트 클라이언트는
별도 이벤트 루프를 돌려 NATS 연결과 충돌하므로, 같은 루프에서
실제 uvicorn을 띄우고 websockets 클라이언트(uvicorn[standard] 동반 의존)로 접속한다.
"""

import asyncio
import json
import socket

import pytest
import uvicorn
import websockets
from lf_eventstore import read_stream
from lf_gateway.config import Config
from lf_gateway.main import create_app

from .conftest import PG_DSN

ENV = "test"
WORLD = "w_main"
PLAYER = "p_tester"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def gateway(nc, conn):
    """실 uvicorn gateway — nc(스트림 초기화 포함)와 같은 루프에서 돈다."""
    cfg = Config(
        nats_url="unused(주입)", env=ENV, pg_dsn=PG_DSN,
        redis_url="redis://localhost:1/0",  # 프레즌스는 best-effort — 실패해도 세션은 산다
        heartbeat_s=0.5,
    )
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(cfg=cfg, nc=nc), host="127.0.0.1", port=port,
                       log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


async def test_command_appends_player_event_and_acks(gateway, conn):
    async with websockets.connect(f"ws://{gateway}/session?player_id={PLAYER}") as ws:
        await ws.send(json.dumps({
            "type": "dm.send", "seq": 1,
            "payload": {"target_actor_id": "a_aria_kim", "text": "응원해요"},
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert ack["type"] == "ack" and ack["seq"] == 1
        event_id = ack["payload"]["event_id"]

    [stored] = await read_stream(conn, WORLD, "player", PLAYER)
    assert stored.envelope["event_id"] == event_id
    assert stored.envelope["type"] == "player.dm.sent"
    assert stored.envelope["payload"]["player_id"] == PLAYER


async def test_follow_set_appends_follow_changed(gateway, conn):
    """팔로우는 로컬 토글이 아니라 세계에 남는 선언이다 (ADR-014 진짜 팔로우 모델)."""
    async with websockets.connect(f"ws://{gateway}/session?player_id={PLAYER}") as ws:
        await ws.send(json.dumps({
            "type": "follow.set", "seq": 1,
            "payload": {"target_actor_id": "a_aria_kim", "following": True},
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert ack["type"] == "ack" and ack["seq"] == 1
        # 철회도 같은 경로 — 마지막 선언이 이긴다
        await ws.send(json.dumps({
            "type": "follow.set", "seq": 2,
            "payload": {"target_actor_id": "a_aria_kim", "following": False},
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert ack["type"] == "ack" and ack["seq"] == 2

    stored = [s.envelope for s in await read_stream(conn, WORLD, "player", PLAYER)]
    assert [e["type"] for e in stored] == ["player.follow.changed"] * 2
    assert stored[0]["payload"]["following"] is True
    assert stored[1]["payload"]["following"] is False


async def test_bad_commands_get_error_frames_not_silence(gateway, conn):
    async with websockets.connect(f"ws://{gateway}/session?player_id={PLAYER}") as ws:
        await ws.send(json.dumps({"type": "world.destroy", "seq": 2, "payload": {}}))
        error = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert error["type"] == "error" and error["seq"] == 2
        assert "알 수 없는 커맨드" in error["payload"]["message"]

        await ws.send(json.dumps({"type": "dm.send", "seq": 3, "payload": {"text": "누구에게?"}}))
        error = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert error["type"] == "error"
        assert "target_actor_id" in error["payload"]["message"]

        # 스키마 위반 (빈 텍스트) — append 검증이 거부한다
        await ws.send(json.dumps({
            "type": "dm.send", "seq": 4,
            "payload": {"target_actor_id": "a_aria_kim", "text": ""},
        }))
        error = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert error["type"] == "error"
        assert "스키마 위반" in error["payload"]["message"]

    assert await read_stream(conn, WORLD, "player", PLAYER) == []  # 아무것도 적재되지 않았다


async def test_actor_reply_is_pushed_to_owning_session(gateway, nc, conn):
    reply_envelope = {
        "event_id": "01JZK7Q3W0000000000000000K",
        "stream": "actor", "type": "actor.message.sent", "schema_version": 1,
        "world_id": WORLD, "actor_id": "a_aria_kim", "tick": 1,
        "occurred_at": "2026-03-01T00:01:00Z",
        "causation_id": None, "correlation_id": "01JZK7Q3W0000000000000000K",
        "payload": {
            "channel": "dm", "target_player_id": PLAYER,
            "text": "고마워요. 오늘은 조금 힘이 나요.",
            "post_id": None, "in_reply_to": "01JZK7Q3W0000000000000000G",
        },
    }
    other = json.loads(json.dumps(reply_envelope))
    other["event_id"] = "01JZK7Q3W0000000000000000L"
    other["payload"]["target_player_id"] = "p_someone_else"

    async with websockets.connect(f"ws://{gateway}/session?player_id={PLAYER}") as ws:
        await asyncio.sleep(0.3)  # 응답 push consumer(DeliverPolicy.NEW) 준비 대기
        js = nc.jetstream()
        subject = f"lf.{ENV}.{WORLD}.actor.message.sent"
        # 남의 응답 먼저 — 필터가 걸러야 한다
        await js.publish(subject, json.dumps(other, ensure_ascii=False).encode())
        await js.publish(subject, json.dumps(reply_envelope, ensure_ascii=False).encode())

        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert frame["type"] == "actor.reply"
        assert frame["payload"]["event_id"] == reply_envelope["event_id"]
        assert frame["payload"]["payload"]["target_player_id"] == PLAYER


def test_actor_comment_is_pushed_to_every_session():
    """액터→액터 댓글 (소셜 루프) — world 포스트의 공개 대화라 모든 세션이 본다.

    수신자 필터(target_player_id)는 나를 향한 응답의 것 — 액터 댓글은
    target_actor_id가 있고 post_id가 있어, 피드 인라인 렌더의 재료로 push된다.
    """
    from lf_gateway.session import reply_frame_for

    comment = {
        "event_id": "01JZK7Q3W0000000000000000N",
        "stream": "actor", "type": "actor.message.sent", "schema_version": 1,
        "world_id": WORLD, "actor_id": "a_junho_park", "tick": 2,
        "occurred_at": "2026-03-01T00:02:00Z",
        "causation_id": "01JZK7Q3W0000000000000000F",
        "correlation_id": "01JZK7Q3W0000000000000000B",
        "payload": {
            "channel": "comment", "target_player_id": None,
            "target_actor_id": "a_aria_kim",
            "text": "그 시작, 응원할게.",
            "post_id": "01JZK7Q3W0000000000000000F",
            "in_reply_to": "01JZK7Q3W0000000000000000F",
        },
    }
    frame = reply_frame_for(json.dumps(comment).encode(), PLAYER, 3)
    assert frame is not None and frame["type"] == "actor.reply"

    # 남을 향한 DM은 여전히 걸러진다 (수신자 단독)
    dm = json.loads(json.dumps(comment))
    dm["payload"].update({"channel": "dm", "target_actor_id": None,
                          "target_player_id": "p_someone_else", "post_id": None})
    assert reply_frame_for(json.dumps(dm).encode(), PLAYER, 4) is None


async def test_invalid_player_id_is_rejected(gateway):
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(f"ws://{gateway}/session?player_id=hacker"):
            pass


@pytest.fixture
async def token_gateway(nc, conn):
    """LF_SESSION_TOKEN이 설정된 gateway — 토큰 게이트 검증용."""
    cfg = Config(
        nats_url="unused(주입)", env=ENV, pg_dsn=PG_DSN,
        redis_url="redis://localhost:1/0", heartbeat_s=0.5,
        session_token="s3cret",
    )
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(cfg=cfg, nc=nc), host="127.0.0.1", port=port,
                       log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


async def test_session_token_gate(token_gateway):
    """토큰이 요구되면: 없거나 틀리면 거부, 맞으면 접속 (스푸핑 완화 게이트)."""
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(f"ws://{token_gateway}/session?player_id={PLAYER}"):
            pass
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(
            f"ws://{token_gateway}/session?player_id={PLAYER}&token=wrong"
        ):
            pass
    async with websockets.connect(
        f"ws://{token_gateway}/session?player_id={PLAYER}&token=s3cret"
    ) as ws:
        await ws.send(json.dumps({"type": "nope", "seq": 1, "payload": {}}))
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert frame["type"] == "error"  # 접속 자체는 살아 있다


async def test_session_token_gate_bearer_header(token_gateway):
    """비브라우저 클라이언트 경로 — Authorization: Bearer도 ?token=과 동등하게 통한다."""
    url = f"ws://{token_gateway}/session?player_id={PLAYER}"
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(
            url, additional_headers={"Authorization": "Bearer wrong"}
        ):
            pass
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(  # Bearer 외 스킴은 토큰으로 치지 않는다
            url, additional_headers={"Authorization": "Basic s3cret"}
        ):
            pass
    async with websockets.connect(
        url, additional_headers={"Authorization": "Bearer s3cret"}
    ) as ws:
        await ws.send(json.dumps({"type": "nope", "seq": 1, "payload": {}}))
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert frame["type"] == "error"  # 접속 자체는 살아 있다
