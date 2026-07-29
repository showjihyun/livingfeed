"""기록된 LLM 출력의 재생 — L0 리플레이 러너의 키스톤 (ADR-011, ADR-021 §4).

## 왜 이것이 먼저인가

지금까지의 L1은 결정 하나하나의 입력을 로그에서 되짚는 방식이었고, 거기서 벽을
만났다: 관계 값처럼 **이벤트 없이 흔들리는 상태**는 점진적 복원으로 닿지 않는다.
남은 길은 하나다 — 세계를 그 tick까지 **다시 돌리는** 것. 그러면 상태는 복원하는
것이 아니라 그냥 다시 생긴다.

문제는 LLM이다. 다시 돌리면서 모델을 다시 부르면 다른 답이 나오고(§4 L3는
보증하지 않는다) 세계는 즉시 갈라진다. 그래서 ADR-011이 정한 답을 그대로 쓴다:

    리플레이 재현성은 RESOLVE에서 확보된다 — LLM 비결정성은 '결정 이벤트'로
    기록되므로 리플레이 시 재호출하지 않는다.

이 모듈이 그 '재호출하지 않음'을 구현한다. 엔진은 평소와 똑같이 AI 클라이언트를
부르고, 이 클라이언트는 모델 대신 **그때 나온 답**을 돌려준다.

## 기록이 없으면 거부한다

가장 위험한 실패 모드는 조용한 폴백이다. 기록을 못 찾았을 때 빈 결과를 돌려주면
엔진은 그것을 'LLM 실패'로 읽고 규칙 경로로 간다 — 세계가 원본과 달라지고, 그
발산이 나중에 '검증 실패'로 보고된다. 없는 사고를 만드는 것이다.

그래서 기록이 없으면 MissingRecordedOutput으로 멈춘다. 반면 **기록된 실패**
(그때 LLM이 답하지 않았다)는 충실히 재생한다 — 그것은 세계의 사실이다.

## 재생의 열쇠

키는 (actor_id, tick, purpose)이고, 같은 키가 한 tick에 여러 번 나오면(예: 받은
봉투마다 답장) 기록된 순서대로 소진한다. 순서의 원천은 actor.decision.made의
global_seq다 — 엔진이 호출한 순서가 곧 적재된 순서이기 때문이다.

원문은 es.decision_traces에 산다 (ADR-021 §5). 즉 **연구 모드로 돌린 구간만
재생할 수 있다** — 샘플링된 기본 모드의 기록으로는 세계를 다시 돌릴 수 없다.
이것이 연구 모드가 존재하는 이유이고, 그 대가로 저장이 이벤트의 6배가 된다.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from lf_actor.client import Inference
from lf_actor.context import Bundle

logger = logging.getLogger("lf.actor.replay_ai")

#: 결정과 그 원문을 한 줄로 — 결정 순서(global_seq)가 재생 순서다
_PLAYBACK_SQL = """
SELECT e.actor_id,
       e.tick,
       e.payload ->> 'purpose'         AS purpose,
       e.payload ->> 'outcome'         AS outcome,
       (e.payload ->> 'trace_retained')::boolean AS retained,
       t.output
FROM es.events e
LEFT JOIN es.decision_traces t ON t.trace_id = e.payload ->> 'trace_id'
WHERE e.world_id = %s AND e.type = 'actor.decision.made'
  AND (%s::bigint IS NULL OR e.tick <= %s)
ORDER BY e.global_seq
"""


class MissingRecordedOutput(Exception):
    """재생할 기록이 없다 — 조용히 폴백하지 않고 멈춘다 (모듈 docstring 참고)."""


@dataclass(frozen=True)
class RecordedCall:
    """그때 한 번의 LLM 호출과 그 결과.

    output이 None인 것은 **그때 LLM이 답하지 않았다**는 기록이다 (outcome이
    hesitated/fallback). 기록 자체가 없는 것과는 다르며, 이쪽은 충실히 재생한다.
    """

    purpose: str
    outcome: str
    output: str | None


class TracePlayback:
    """한 세계의 기록된 LLM 출력 — (actor, tick, purpose)별 순서 큐."""

    def __init__(self, calls: dict[tuple[str, int, str], deque[RecordedCall]]) -> None:
        self._calls = calls

    @classmethod
    async def load(
        cls, conn: AsyncConnection, world_id: str, *, through_tick: int | None = None
    ) -> TracePlayback:
        cur = await conn.execute(_PLAYBACK_SQL, (world_id, through_tick, through_tick))
        calls: dict[tuple[str, int, str], deque[RecordedCall]] = defaultdict(deque)
        missing_traces = 0
        for actor_id, tick, purpose, outcome, retained, output in await cur.fetchall():
            if retained is False and outcome == "acted":
                # 원문이 샘플링에서 빠졌다 — 그 호출은 재생할 수 없다.
                # 여기서 세지 않고 넘기면 나중에 조용한 폴백이 된다.
                missing_traces += 1
                continue
            calls[(actor_id, tick, purpose)].append(
                RecordedCall(purpose=purpose, outcome=outcome, output=output)
            )
        if missing_traces:
            logger.warning(
                "원문이 없는 결정 %d건 — 그 구간은 재생할 수 없다 (연구 모드가 아니었다,"
                " ADR-021 §5)", missing_traces,
            )
        return cls(dict(calls))

    def take(self, actor_id: str, tick: int, purpose: str) -> RecordedCall:
        queue = self._calls.get((actor_id, tick, purpose))
        if not queue:
            raise MissingRecordedOutput(
                f"재생할 기록이 없다: actor={actor_id} tick={tick} purpose={purpose!r}."
                " 원본과 다른 호출을 하고 있거나, 그 구간이 연구 모드가 아니었다."
                " 조용한 폴백은 세계를 갈라 놓고 그 발산이 '검증 실패'로 오독된다."
            )
        return queue.popleft()

    @property
    def remaining(self) -> int:
        """아직 소진되지 않은 기록 — 재생이 끝난 뒤 0이어야 원본과 같은 횟수로 불렀다."""
        return sum(len(q) for q in self._calls.values())


class ReplayAiClient:
    """모델 대신 기록을 돌려주는 AI 클라이언트 (AiRuntimeClient와 같은 계약).

    엔진은 이것이 재생인지 모른다 — 평소처럼 부르고 평소처럼 받는다. 그래서
    리플레이가 '특별한 경로'가 아니라 **같은 경로에 다른 입력**이 된다.
    """

    def __init__(self, playback: TracePlayback) -> None:
        self._playback = playback

    async def decide_action(
        self, bundle: Bundle, output_schema: dict[str, Any], *,
        tier: str, actor_id: str, tick: int,
    ) -> Inference:
        return self._structured(bundle, actor_id, tick)

    async def reflect(
        self, bundle: Bundle, output_schema: dict[str, Any], *, actor_id: str, tick: int
    ) -> Inference:
        return self._structured(bundle, actor_id, tick)

    async def converse(
        self, bundle: Bundle, *, tier: str, actor_id: str, tick: int
    ) -> Inference:
        call = self._playback.take(actor_id, tick, bundle.purpose)
        # converse의 기록은 답장 원문 그대로다 (JSON이 아니다 — _record_decision 참고)
        return Inference(call.output, model=_REPLAY_MODEL)

    def _structured(self, bundle: Bundle, actor_id: str, tick: int) -> Inference:
        call = self._playback.take(actor_id, tick, bundle.purpose)
        if call.output is None:
            return Inference(model=_REPLAY_MODEL)  # 그때도 답하지 않았다
        return Inference(json.loads(call.output), model=_REPLAY_MODEL)


#: 재생 호출의 모델 이름 — 실제 모델을 부르지 않았다는 사실이 기록에 남아야 한다.
#: 원본의 모델 이름을 그대로 흉내내면 재생 세계가 원본인 척하게 된다.
_REPLAY_MODEL = "replay:recorded"
