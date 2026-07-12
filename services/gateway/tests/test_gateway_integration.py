"""gateway SSE·롱폴링 NATS 통합 — JetStream 구독→필터→전송 (ADR-010).

NATS 필요 (없으면 skip — conftest 참고). 발행은 테스트가 relay를 흉내내
LF_FEED subject로 직접 넣는다 (relay 자체는 dispatcher 테스트가 검증).
"""

import asyncio
import json
from pathlib import Path

import httpx
from lf_gateway.config import Config
from lf_gateway.feed_stream import FeedFilter, sse_frames
from lf_gateway.main import create_app

SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "feed.post.published.001.json"
    ).read_text(encoding="utf-8")
)
ENV = "test"
SUBJECT = f"lf.{ENV}.{SAMPLE['world_id']}.{SAMPLE['type']}"
EPOCH_CURSOR = "0" * 26  # 1970년 — 전체 리플레이 커서


def make_cfg() -> Config:
    return Config(nats_url="unused(주입)", env=ENV, heartbeat_s=0.5)


async def publish_sample(nc) -> None:
    js = nc.jetstream()
    await js.publish(
        SUBJECT,
        json.dumps(SAMPLE, ensure_ascii=False).encode(),
        headers={"Nats-Msg-Id": SAMPLE["event_id"]},
    )


def make_client(nc) -> httpx.AsyncClient:
    app = create_app(cfg=make_cfg(), nc=nc)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    )


async def test_poll_replays_from_cursor_and_advances_it(nc):
    await publish_sample(nc)
    async with make_client(nc) as client:
        r = await client.get(
            "/poll/feed", params={"cursor": EPOCH_CURSOR, "wait_s": 3}
        )
        assert r.status_code == 200
        body = r.json()
        assert [i["event_id"] for i in body["items"]] == [SAMPLE["event_id"]]
        assert body["next_cursor"] == SAMPLE["event_id"]

        # 전진한 커서로 다시 폴링 — 이미 본 이벤트는 오지 않는다
        r2 = await client.get(
            "/poll/feed", params={"cursor": body["next_cursor"], "wait_s": 0.5}
        )
        assert r2.json()["items"] == []
        assert r2.json()["next_cursor"] == SAMPLE["event_id"]


async def test_poll_filters_visibility(nc):
    await publish_sample(nc)
    async with make_client(nc) as client:
        r = await client.get(
            "/poll/feed",
            params={"cursor": EPOCH_CURSOR, "types": "personal,private", "wait_s": 0.5},
        )
        assert r.json()["items"] == []


async def test_sse_frames_stream_event_with_post_id_as_sse_id(nc):
    """SSE 프레임 생성기를 직접 소비한다 — httpx ASGITransport는 무한 스트림
    응답을 버퍼링해 SSE를 읽을 수 없다. HTTP 계층 전체는 E2E 스모크가 검증한다.
    """
    await publish_sample(nc)
    flt = FeedFilter(world_id="w_main", kinds=frozenset({"world"}), cursor=EPOCH_CURSOR)

    async def read_first_event() -> str:
        async for frame in sse_frames(nc.jetstream(), make_cfg(), flt):
            if "data:" in frame:
                return frame
        raise AssertionError("SSE 이벤트가 오지 않았다")

    frame = await asyncio.wait_for(read_first_event(), timeout=10)
    lines = dict(
        line.split(": ", 1) for line in frame.strip().splitlines() if ": " in line
    )
    assert lines["id"] == SAMPLE["event_id"]
    assert lines["event"] == "feed.post.published"
    assert json.loads(lines["data"])["payload"]["title"] == SAMPLE["payload"]["title"]


async def test_bad_params_rejected(nc):
    async with make_client(nc) as client:
        assert (await client.get("/poll/feed", params={"types": "doom"})).status_code == 400
        assert (await client.get("/poll/feed", params={"cursor": "nope"})).status_code == 400
