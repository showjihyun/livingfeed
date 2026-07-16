"""피드 내레이터 — 고드라마 포스트의 본문을 서사 문장으로 (ADR-018 narrate, ADR-014).

편집 2단의 표현 계층이다: 승격(임계·점수)은 결정적 규칙이 정하고, 이 모듈은
이미 승격이 확정된 포스트의 본문만 다듬는다. 실패는 None — 템플릿 본문이
그대로 나간다 (조용한 강등, 세계는 계속 흐른다). rule 프로바이더는 narrate를
지원하지 않으므로 dev 기본에서는 언제나 템플릿이다 (결정성 유지).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import nats.errors
from lf_eventstore import new_ulid
from nats.aio.client import Client as NatsClient

logger = logging.getLogger("lf.feed.narrator")

#: 승격 확정 후의 본문 다듬기라 tick 경로가 아니다 — 여유 있게, 그러나 무한정은 아니게
DEFAULT_TIMEOUT_S = 8.0

_NARRATE_SYSTEM = (
    "너는 살아있는 세계 피드의 편집자다. 사건 요약을 2~3문장의 서사 본문으로 "
    "다시 쓴다 — 과장 없이, 인물의 결이 느껴지게. 원문에 없는 사실을 지어내지 "
    "않는다. 이름은 원문 표기 그대로 쓴다. a_ 로 시작하는 식별자는 사람 이름이 "
    "아니다 — 문장에 넣지 말고, 이름을 모르는 인물은 '누군가'로 둔다."
)

_BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"body": {"type": "string", "minLength": 1, "maxLength": 2000}},
    "required": ["body"],
    "additionalProperties": False,
}


class FeedNarrator:
    """AI Runtime narrate 태스크의 최소 클라이언트 (NATS request-reply, ADR-018)."""

    def __init__(self, nc: NatsClient, env: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._nc = nc
        self._subject = f"lf.{env}.ai.infer"
        self._timeout = timeout_s

    async def narrate(
        self, *, title: str, body: str, participants: list[str], world_id: str, tick: int
    ) -> str | None:
        """제목·원문·참여자로 서사 본문을 요청한다. 실패는 None — 템플릿 폴백."""
        trace_id = new_ulid()
        user = "\n".join([
            f"## 제목\n{title}",
            f"## 원문 (사건 요약)\n{body}",
            f"## 등장 인물\n{', '.join(participants) or '(없음)'}",
            "",
            '위 사건을 2~3문장의 피드 본문으로 다시 써라. 출력은 {"body": "..."} JSON 하나뿐이다.',
        ])
        request = {
            "task": "narrate",
            "bundle": {"system": _NARRATE_SYSTEM, "user": user, "trace_id": trace_id},
            "output_schema": _BODY_SCHEMA,
            "actor_tier": "system",  # narrate×system 라우팅 (ADR-018 표)
            "trace": {"trace_id": trace_id, "world_id": world_id, "tick": tick},
        }
        try:
            reply = await self._nc.request(
                self._subject,
                json.dumps(request, ensure_ascii=False).encode(),
                timeout=self._timeout,
            )
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError) as e:
            logger.info("내레이션 생략 (AI Runtime 미응답): %s", e)
            return None
        response = json.loads(reply.data)
        if not response.get("ok"):
            logger.info("내레이션 생략: %s", response.get("error"))
            return None
        output = response.get("output")
        text = output.get("body") if isinstance(output, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
        return text.strip()
