"""서사 푸시 알림 (push.py, plan/11 §D1·§알림 정책).

세 층을 겨눈다:
- 문구 빌더(순수) — 이름 그라운딩·이/가 조사·80자 절단·dm/comment 구분·비대상 제외
- 구독 API — Redis hash 저장/해지/검증 (ASGITransport + 주입 redis, admin 테스트 선례)
- 발송 정책 — 프레즌스 제외·410 자동 제거·실패 무시 (web push 호출은 스텁),
  durable 워커 배선은 NATS 통합으로 한 번
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest
from lf_gateway.config import Config
from lf_gateway.main import create_app
from lf_gateway.push import (
    ActorNames,
    SubscriptionGone,
    build_notification,
    clip_preview,
    deliver,
    push_key,
    run_push_worker,
    subject_particle,
)
from lf_gateway.session import presence_key, presence_value

WORLD = "w_main"
PLAYER = "p_away_tester"

NAMES = {"a_mint": "민트", "a_jihoon": "지훈"}


def name_of(actor_id: str | None) -> str:
    return NAMES.get(actor_id or "", "누군가")


def envelope(**payload_over) -> dict:
    payload = {
        "channel": "comment",
        "target_player_id": PLAYER,
        "text": "그 말이 오늘 하루를 버티게 했어요.",
        "post_id": "01JZK7Q3W0000000000000000G",
        "in_reply_to": "01JZK7Q3W0000000000000000G",
        **payload_over,
    }
    return {
        "event_id": "01JZK7Q3W0000000000000000K",
        "type": "actor.message.sent",
        "world_id": WORLD,
        "actor_id": "a_mint",
        "tick": 44,
        "payload": payload,
    }


# ── 문구 빌더 (순수) — 서사형, 다크 패턴 금지 ────────────────────────────────


def test_comment_reply_notification_grounds_actor_name():
    note = build_notification(envelope(), name_of)
    assert note == {
        "title": "민트가 당신에게 답했어요",
        "body": "그 말이 오늘 하루를 버티게 했어요.",
    }


def test_dm_notification_uses_greeting_phrase():
    note = build_notification(envelope(channel="dm", post_id=None), name_of)
    assert note["title"] == "민트의 안부가 도착했어요"


def test_unknown_actor_falls_back_to_someone():
    env = envelope(channel="dm")
    env["actor_id"] = "a_stranger"
    assert build_notification(env, name_of)["title"] == "누군가의 안부가 도착했어요"


def test_subject_particle_follows_final_consonant():
    assert subject_particle("민지") == "가"
    assert subject_particle("지훈") == "이"
    assert subject_particle("Zed") == "이(가)"  # 한글 밖 이름은 병기 폴백
    env = envelope()
    env["actor_id"] = "a_jihoon"
    assert build_notification(env, name_of)["title"] == "지훈이 당신에게 답했어요"


def test_body_clipped_to_80_chars():
    long_text = "가" * 81
    note = build_notification(envelope(text=long_text), name_of)
    assert note["body"] == "가" * 80 + "…"
    assert build_notification(envelope(text="가" * 80), name_of)["body"] == "가" * 80


def test_preview_collapses_whitespace():
    assert clip_preview("첫 줄\n둘째  줄") == "첫 줄 둘째 줄"


def test_actor_to_actor_message_is_not_pushed():
    """액터→액터 댓글은 그 유저의 서사가 아니다 — §D1의 경계."""
    note = build_notification(
        envelope(target_player_id=None, target_actor_id="a_jihoon"), name_of
    )
    assert note is None


def test_actor_names_loads_personas_and_falls_back(tmp_path: Path):
    (tmp_path / "mint.yaml").write_text("id: a_mint\nname: 민트\n", encoding="utf-8")
    names = ActorNames(tmp_path)
    assert names.get("a_mint") == "민트"
    assert names.get("a_unknown") == "누군가"
    assert names.get(None) == "누군가"
    # 디렉터리가 없어도 죽지 않는다 — 전달 채널이 세계를 멈추지 않는다
    assert ActorNames(tmp_path / "no_dir").get("a_mint") == "누군가"


# ── 구독 API — Redis hash 저장/해지 (es 적재 없음) ───────────────────────────


def make_client(redis, *, vapid_public: str | None = None) -> httpx.AsyncClient:
    cfg = Config(nats_url="unused(주입)", env="test", vapid_public_key=vapid_public)
    app = create_app(cfg, redis=redis)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def sub_doc(endpoint: str = "https://push.example.com/ep-1") -> dict:
    return {
        "endpoint": endpoint,
        "expirationTime": None,  # 표준 toJSON()의 결 — 저장 시 보존된다
        "keys": {"p256dh": "BLc4xRzKlKORKWlbdgFaBrrPK3ydW", "auth": "5I2Bu2oKdyy9CwL8QVF0NQ"},
    }


async def test_subscribe_stores_multiple_browsers_per_player(redis):
    async with make_client(redis) as client:
        for endpoint in ("https://push.example.com/ep-1", "https://push.example.com/ep-2"):
            resp = await client.post("/push/subscribe", json={
                "player_id": PLAYER, "world_id": WORLD, "subscription": sub_doc(endpoint),
            })
            assert resp.status_code == 204
        # 같은 endpoint 재구독은 갱신이다 — 필드가 늘지 않는다
        resp = await client.post("/push/subscribe", json={
            "player_id": PLAYER, "world_id": WORLD,
            "subscription": sub_doc("https://push.example.com/ep-1"),
        })
        assert resp.status_code == 204

    stored = await redis.hgetall(push_key(WORLD, PLAYER))
    assert sorted(stored) == [b"https://push.example.com/ep-1", b"https://push.example.com/ep-2"]
    doc = json.loads(stored[b"https://push.example.com/ep-1"])
    assert doc["keys"]["auth"] == "5I2Bu2oKdyy9CwL8QVF0NQ"
    assert doc["expirationTime"] is None  # 미지 키 보존 — 표준 구독 JSON 그대로


async def test_unsubscribe_removes_only_that_endpoint(redis):
    async with make_client(redis) as client:
        for endpoint in ("https://push.example.com/ep-1", "https://push.example.com/ep-2"):
            await client.post("/push/subscribe", json={
                "player_id": PLAYER, "world_id": WORLD, "subscription": sub_doc(endpoint),
            })
        resp = await client.request("DELETE", "/push/subscribe", json={
            "player_id": PLAYER, "world_id": WORLD,
            "endpoint": "https://push.example.com/ep-1",
        })
        assert resp.status_code == 200 and resp.json() == {"removed": True}
        again = await client.request("DELETE", "/push/subscribe", json={
            "player_id": PLAYER, "world_id": WORLD,
            "endpoint": "https://push.example.com/ep-1",
        })
        assert again.json() == {"removed": False}  # 이미 없다 — 멱등

    assert await redis.hkeys(push_key(WORLD, PLAYER)) == [b"https://push.example.com/ep-2"]


@pytest.mark.parametrize(
    "body_over",
    [
        {"player_id": "observer"},  # p_ 접두 위반
        {"world_id": "main"},  # w_ 접두 위반
        # https 아님
        {"subscription": {"endpoint": "http://x.example/ep",
                          "keys": {"p256dh": "x", "auth": "y"}}},
        # auth 누락
        {"subscription": {"endpoint": "https://push.example.com/ep", "keys": {"p256dh": "x"}}},
    ],
)
async def test_subscribe_validation_rejects_bad_bodies(redis, body_over):
    body = {"player_id": PLAYER, "world_id": WORLD, "subscription": sub_doc(), **body_over}
    async with make_client(redis) as client:
        resp = await client.post("/push/subscribe", json=body)
    assert resp.status_code == 422
    assert await redis.hkeys(push_key(WORLD, PLAYER)) == []  # 거부된 구독은 저장되지 않는다


async def test_vapid_key_endpoint_reflects_configuration(redis):
    async with make_client(redis) as client:
        assert (await client.get("/push/vapid-key")).status_code == 404  # dev 기본 꺼짐
    async with make_client(redis, vapid_public="BTestServerKey") as client:
        resp = await client.get("/push/vapid-key")
    assert resp.status_code == 200 and resp.json() == {"key": "BTestServerKey"}


# ── 세션 토큰 게이트 — 구독 하이재킹 방지 (인프라 없이 최소 redis 대역) ──────────


class _StoreRedis:
    """게이트 검증만 겨눈 최소 redis 대역 — 통과 경로의 hset/hdel만 받는다."""

    def __init__(self) -> None:
        self.h: dict[str, dict] = {}

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value
        return 1

    async def hdel(self, key, field):
        return int(self.h.get(key, {}).pop(field, None) is not None)


def gated_client(token: str | None) -> httpx.AsyncClient:
    cfg = Config(nats_url="unused(주입)", env="test", session_token=token)
    app = create_app(cfg, redis=_StoreRedis())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_subscribe_requires_session_token_when_set():
    """LF_SESSION_TOKEN이 서면 구독 저장/해지는 토큰을 요구한다 — 인증 없이 열려 있으면
    공격자가 남의 player_id로 자기 endpoint를 심어 그 유저의 DM 미리보기를 가로챈다."""
    sub = {"player_id": PLAYER, "world_id": WORLD, "subscription": sub_doc()}
    unsub = {"player_id": PLAYER, "world_id": WORLD, "endpoint": sub_doc()["endpoint"]}
    async with gated_client("s3cret") as client:
        assert (await client.post("/push/subscribe", json=sub)).status_code == 403
        bad = await client.post(
            "/push/subscribe", json=sub, headers={"authorization": "Bearer nope"}
        )
        assert bad.status_code == 403
        ok = await client.post(
            "/push/subscribe", json=sub, headers={"authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 204
        # WS /session과 같은 ?token= 형태도 허용 (브라우저 경로)
        assert (await client.post("/push/subscribe?token=s3cret", json=sub)).status_code == 204
        # 해지도 같은 게이트
        assert (await client.request("DELETE", "/push/subscribe", json=unsub)).status_code == 403
        gone = await client.request(
            "DELETE", "/push/subscribe", json=unsub, headers={"authorization": "Bearer s3cret"}
        )
        assert gone.status_code == 200
        # 공개키는 게이트 밖 — 사설 데이터가 아니다 (FE가 구독 전에 읽어야 한다)
        assert (await client.get("/push/vapid-key")).status_code == 404


async def test_subscribe_open_when_token_unset():
    """dev 기본(미설정)은 열림 — 로컬 개발이 토큰 없이 돈다 (기존 계약 유지)."""
    sub = {"player_id": PLAYER, "world_id": WORLD, "subscription": sub_doc()}
    async with gated_client(None) as client:
        assert (await client.post("/push/subscribe", json=sub)).status_code == 204


# ── 발송 정책 — 프레즌스 제외·소멸 구독 자동 제거·실패 무시 ──────────────────


class RecordingSender:
    def __init__(self, fail: dict[str, Exception] | None = None):
        self.sent: list[tuple[str, dict]] = []
        self._fail = fail or {}

    async def __call__(self, subscription: dict, note: dict) -> None:
        exc = self._fail.get(subscription["endpoint"])
        if exc is not None:
            raise exc
        self.sent.append((subscription["endpoint"], note))


def dir_names(tmp_path: Path) -> ActorNames:
    (tmp_path / "mint.yaml").write_text("id: a_mint\nname: 민트\n", encoding="utf-8")
    return ActorNames(tmp_path)


async def seed_subscriptions(redis, *endpoints: str) -> None:
    for endpoint in endpoints:
        await redis.hset(
            push_key(WORLD, PLAYER), endpoint, json.dumps(sub_doc(endpoint), ensure_ascii=False)
        )


async def test_deliver_sends_to_every_browser_of_absent_player(redis, tmp_path):
    await seed_subscriptions(redis, "https://push.example.com/a", "https://push.example.com/b")
    send = RecordingSender()
    sent = await deliver(envelope(), redis=redis, names=dir_names(tmp_path), send=send)
    assert sent == 2
    assert {e for e, _ in send.sent} == {"https://push.example.com/a", "https://push.example.com/b"}
    assert send.sent[0][1]["title"] == "민트가 당신에게 답했어요"


async def test_deliver_skips_player_present_in_session(redis, tmp_path):
    """접속 중이면 보내지 않는다 — 이미 WS로 보고 있다 (알림 신뢰 자산 보호)."""
    await seed_subscriptions(redis, "https://push.example.com/a")
    await redis.set(presence_key(WORLD, PLAYER), presence_value(3), ex=3600)
    send = RecordingSender()
    assert await deliver(envelope(), redis=redis, names=dir_names(tmp_path), send=send) == 0
    assert send.sent == []


async def test_deliver_removes_gone_subscription_and_keeps_rest(redis, tmp_path):
    await seed_subscriptions(redis, "https://push.example.com/dead", "https://push.example.com/live")
    send = RecordingSender(fail={"https://push.example.com/dead": SubscriptionGone("410")})
    await deliver(envelope(), redis=redis, names=dir_names(tmp_path), send=send)
    assert await redis.hkeys(push_key(WORLD, PLAYER)) == [b"https://push.example.com/live"]


async def test_deliver_survives_transient_send_failure(redis, tmp_path):
    """일시 실패는 로그만 — 구독은 남고, 다른 브라우저 발송도 계속된다."""
    await seed_subscriptions(redis, "https://push.example.com/flaky", "https://push.example.com/ok")
    send = RecordingSender(fail={"https://push.example.com/flaky": RuntimeError("서비스 500")})
    sent = await deliver(envelope(), redis=redis, names=dir_names(tmp_path), send=send)
    assert sent == 1
    assert sorted(await redis.hkeys(push_key(WORLD, PLAYER))) == [
        b"https://push.example.com/flaky", b"https://push.example.com/ok",
    ]


async def test_deliver_without_subscription_is_noop(redis, tmp_path):
    send = RecordingSender()
    assert await deliver(envelope(), redis=redis, names=dir_names(tmp_path), send=send) == 0


# ── 발송 워커 배선 — LF_ACTOR durable pull 소비 (NATS 통합) ──────────────────


async def test_worker_consumes_stream_and_delivers(nc, redis, tmp_path, monkeypatch):
    import lf_gateway.push as push_mod

    monkeypatch.setattr(push_mod, "FETCH_TIMEOUT_S", 0.3)  # 테스트 응답성
    (tmp_path / "mint.yaml").write_text("id: a_mint\nname: 민트\n", encoding="utf-8")
    cfg = Config(
        nats_url="unused(주입)", env="test",
        personas_dir=tmp_path, push_durable="gwpush-test",
    )
    js = nc.jetstream()
    await seed_subscriptions(redis, "https://push.example.com/worker")
    send = RecordingSender()
    task = asyncio.create_task(run_push_worker(js, redis, cfg, send=send))
    try:
        # DeliverPolicy.NEW — durable이 선 다음의 발행만 받는다. 설 때까지 대기.
        for _ in range(100):
            try:
                await js.consumer_info("LF_ACTOR", "gwpush-test")
                break
            except Exception:
                await asyncio.sleep(0.05)
        await js.publish(
            f"lf.test.{WORLD}.actor.message.sent",
            json.dumps(envelope(), ensure_ascii=False).encode(),
        )
        for _ in range(100):
            if send.sent:
                break
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert send.sent == [(
        "https://push.example.com/worker",
        {"title": "민트가 당신에게 답했어요", "body": "그 말이 오늘 하루를 버티게 했어요."},
    )]
