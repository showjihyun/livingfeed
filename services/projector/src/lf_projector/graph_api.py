"""graph query API — Kuzu는 임베디드라 다중 프로세스가 직접 못 읽는다 (ADR-006).

kuzu-projector 프로세스가 NATS request-reply로 얇은 정형 질의를 중재한다.
op는 고정 목록만 — 원시 Cypher는 노출하지 않는다 (교체 가능성 보존).
queue group으로 read-replica projector 수평 확장이 가능하다 (ADR-006 완화책).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import nats
import nats.errors
from nats.aio.client import Client as NatsClient

if TYPE_CHECKING:  # 런타임 미임포트 — gateway/feed-api가 kuzu 없이 클라이언트만 쓴다
    from lf_projector.graph import RelGraph

logger = logging.getLogger("lf.projector.graph_api")

QUEUE_GROUP = "kuzu-graph"


def graph_query_subject(env: str) -> str:
    """graph query API subject — ai.infer(ADR-018)와 같은 request-reply 계열."""
    return f"lf.{env}.graph.query"


async def serve_graph_api(
    nc: NatsClient, graph: RelGraph, env: str, *, stop: asyncio.Event | None = None
) -> None:
    stop = stop or asyncio.Event()

    async def handle(msg: Any) -> None:
        try:
            request = json.loads(msg.data)
            op = request.get("op")
            world_id = request["world_id"]
            if op == "player_graph":
                output = graph.player_graph(world_id, request["player_id"])
            elif op == "world_graph":
                # 바닥값·상한 기본은 RelGraph의 단일 정의 — 요청은 재정의만 싣는다
                kwargs: dict[str, Any] = {}
                if "min_weight" in request:
                    kwargs["min_weight"] = min(max(float(request["min_weight"]), 0.0), 1.0)
                if "limit" in request:
                    kwargs["limit"] = min(max(int(request["limit"]), 1), 500)
                output = graph.world_graph(world_id, **kwargs)
            elif op == "proximity":
                output = {
                    "scores": graph.proximity(
                        world_id, request["from_id"], list(request["to_ids"])
                    )
                }
            elif op == "tension_pairs":
                output = {
                    "pairs": graph.tension_pairs(
                        world_id,
                        min_resentment=float(request.get("min_resentment", 0.1)),
                        limit=int(request.get("limit", 5)),
                    )
                }
            else:
                raise ValueError(
                    f"알 수 없는 op: {op!r} "
                    "(지원: player_graph, world_graph, proximity, tension_pairs)"
                )
            response = {"ok": True, "output": output}
        except Exception as e:  # 명시적 오류 응답 — 조용한 유실 금지
            logger.exception("graph query 실패")
            response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        await msg.respond(json.dumps(response, ensure_ascii=False).encode())

    subject = graph_query_subject(env)
    await nc.subscribe(subject, queue=QUEUE_GROUP, cb=handle)
    logger.info("graph query API 대기 — subject=%s", subject)
    await stop.wait()


class GraphQueryClient:
    """엔진·서비스가 그래프를 읽는 유일한 경로 (ADR-006 §배치 구조).

    실패는 빈 결과 — 그래프는 프로젝션(최적화)이므로 호출측 경로를 죽이면 안 된다.
    """

    def __init__(self, nc: NatsClient, env: str, *, timeout_s: float = 1.0) -> None:
        self._nc = nc
        self._subject = graph_query_subject(env)
        self._timeout = timeout_s

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            reply = await self._nc.request(
                self._subject,
                json.dumps(payload, ensure_ascii=False).encode(),
                timeout=self._timeout,
            )
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError) as e:
            logger.warning("graph query 미가용(우회): %s", e)
            return None
        response = json.loads(reply.data)
        if not response.get("ok"):
            logger.warning("graph query 거부(우회): %s", response.get("error"))
            return None
        return response.get("output")

    async def player_graph(self, world_id: str, player_id: str) -> dict[str, Any] | None:
        return await self._request(
            {"op": "player_graph", "world_id": world_id, "player_id": player_id}
        )

    async def world_graph(
        self,
        world_id: str,
        *,
        min_weight: float | None = None,
        limit: int | None = None,
    ) -> dict[str, Any] | None:
        """세계 전체 관계망 — None 파라미터는 프로젝터 기본(단일 정의)을 따른다.

        미가용이면 None — 호출측은 available=False로 강등한다 (player_graph와 동일).
        """
        payload: dict[str, Any] = {"op": "world_graph", "world_id": world_id}
        if min_weight is not None:
            payload["min_weight"] = min_weight
        if limit is not None:
            payload["limit"] = limit
        return await self._request(payload)

    async def proximity(
        self, world_id: str, from_id: str, to_ids: list[str]
    ) -> dict[str, float]:
        output = await self._request(
            {"op": "proximity", "world_id": world_id, "from_id": from_id, "to_ids": to_ids}
        )
        if output is None:
            return {}
        return {k: float(v) for k, v in output.get("scores", {}).items()}

    async def tension_pairs(
        self, world_id: str, *, min_resentment: float = 0.1, limit: int = 5
    ) -> list[list[Any]]:
        output = await self._request(
            {
                "op": "tension_pairs", "world_id": world_id,
                "min_resentment": min_resentment, "limit": limit,
            }
        )
        return list(output.get("pairs", [])) if output else []
