"""Kuzu 관계 그래프 프로젝션 — 스키마·업서트·정형 질의 (ADR-006).

임베디드: projector 프로세스 안에 세계당 1개 DB 디렉터리가 산다.
프로젝션은 소모품 — 재구축은 디렉터리 삭제 후 replay (ADR-003 계약 3).
질의는 정형 op만 노출한다 (Cypher 부분집합 제한 — Kuzu 교체 가능성 보존).

주의: kuzu 호출은 동기다. Phase 1 그래프(수백 엣지)에서는 이벤트 루프에서
직접 불러도 무시 가능한 지연이며, 스레드 분리는 규모가 생기면 (ADR-006 완화책의
read-replica projector와 함께) 도입한다.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import kuzu

logger = logging.getLogger("lf.projector.graph")

#: 관계도(strength) — FE 노드 크기와 피드 관계 근접도 랭킹이 공유하는 단일 정의.
#: 친밀이 절반, 신뢰(양수만), 비중 순 — 계수 변경은 ADR-014/016 개정 사항.
def strength(trust: float, intimacy: float, salience: float) -> float:
    return round(min(1.0, 0.5 * intimacy + 0.3 * max(trust, 0.0) + 0.2 * salience), 4)


_SCHEMA = (
    "CREATE NODE TABLE IF NOT EXISTS Actor("
    "id STRING, name STRING, archetype STRING, PRIMARY KEY(id))",
    # 엣지 차원은 ADR-016이 정의 — salience는 컨텍스트 우선순위용으로 함께 둔다
    "CREATE REL TABLE IF NOT EXISTS RELATES(FROM Actor TO Actor, "
    "trust DOUBLE, intimacy DOUBLE, respect DOUBLE, attraction DOUBLE, "
    "resentment DOUBLE, salience DOUBLE, stage STRING, updated_tick INT64)",
)

_UPSERT_ACTOR = (
    "MERGE (a:Actor {id: $id}) "
    "ON CREATE SET a.name = $name, a.archetype = $archetype"
)

# 표준 Cypher MERGE는 전체 패턴 미매치 시 노드까지 생성하려 든다 —
# 노드는 먼저 MATCH하고 엣지만 MERGE해야 PK 충돌 없이 멱등이다
_UPSERT_EDGE = (
    "MATCH (a:Actor {id: $from_id}), (b:Actor {id: $to_id}) "
    "MERGE (a)-[r:RELATES]->(b) "
    "SET r.trust = $trust, r.intimacy = $intimacy, r.respect = $respect, "
    "r.attraction = $attraction, r.resentment = $resentment, "
    "r.salience = $salience, r.stage = $stage, r.updated_tick = $tick"
)

_SET_STAGE = (
    "MATCH (a:Actor {id: $from_id})-[r:RELATES]->(b:Actor {id: $to_id}) "
    "SET r.stage = $stage, r.updated_tick = $tick"
)

_TOUCHING = (
    "MATCH (a:Actor)-[r:RELATES]->(b:Actor) "
    "WHERE a.id = $id OR b.id = $id "
    "RETURN a.id, b.id, r.trust, r.intimacy, r.respect, r.attraction, "
    "r.resentment, r.salience, r.stage, r.updated_tick"
)

_PAIR = (
    "MATCH (a:Actor)-[r:RELATES]->(b:Actor) "
    "WHERE (a.id = $x AND b.id = $y) OR (a.id = $y AND b.id = $x) "
    "RETURN r.trust, r.intimacy, r.salience"
)

_TENSION = (
    "MATCH (a:Actor)-[r:RELATES]->(b:Actor) "
    "WHERE r.resentment >= $min_resentment "
    "RETURN a.id, b.id, r.resentment, r.trust "
    "ORDER BY r.resentment DESC LIMIT $limit"
)

_ALL_EDGES = "MATCH (a:Actor)-[r:RELATES]->(b:Actor) RETURN a.id, b.id"


class RelGraph:
    """세계별 Kuzu DB 핸들 캐시 + 정형 연산."""

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)
        self._conns: dict[str, kuzu.Connection] = {}
        self._dbs: dict[str, kuzu.Database] = {}

    def _conn(self, world_id: str) -> kuzu.Connection:
        conn = self._conns.get(world_id)
        if conn is not None:
            return conn
        self._base.mkdir(parents=True, exist_ok=True)
        db = kuzu.Database(str(self._base / world_id))
        conn = kuzu.Connection(db)
        for statement in _SCHEMA:
            conn.execute(statement)
        self._dbs[world_id] = db
        self._conns[world_id] = conn
        return conn

    def ensure_actor(self, world_id: str, actor_id: str) -> None:
        # 이름·아키타입은 액터 프로필 이벤트가 생기면 채워진다 — 지금은 id가 이름이다
        archetype = "player" if actor_id.startswith("p_") else "actor"
        self._conn(world_id).execute(
            _UPSERT_ACTOR, {"id": actor_id, "name": actor_id, "archetype": archetype}
        )

    def apply_state_changed(self, world_id: str, envelope: dict[str, Any]) -> None:
        """relationship.state.changed → 엣지 현재 상태 덮어쓰기 (자연 멱등, ADR-003 계약 1)."""
        p = envelope["payload"]
        self.ensure_actor(world_id, p["from_id"])
        self.ensure_actor(world_id, p["to_id"])
        self._conn(world_id).execute(
            _UPSERT_EDGE,
            {
                "from_id": p["from_id"],
                "to_id": p["to_id"],
                **{k: float(v) for k, v in p["dimensions"].items()},
                "salience": float(p["salience"]),
                "stage": p["stage"],
                "tick": int(envelope["tick"]),
            },
        )

    def apply_milestone(self, world_id: str, envelope: dict[str, Any]) -> None:
        """relationship.milestone.reached → stage 전이 반영 (차원은 state.changed의 몫)."""
        p = envelope["payload"]
        self.ensure_actor(world_id, p["from_id"])
        self.ensure_actor(world_id, p["to_id"])
        conn = self._conn(world_id)
        result = conn.execute(
            _SET_STAGE,
            {
                "from_id": p["from_id"], "to_id": p["to_id"],
                "stage": p["stage"], "tick": int(envelope["tick"]),
            },
        )
        # 엣지가 아직 없으면(마일스톤이 첫 이벤트) 중립 차원으로 생성
        if result.get_num_tuples() == 0:
            conn.execute(
                _UPSERT_EDGE,
                {
                    "from_id": p["from_id"], "to_id": p["to_id"],
                    "trust": 0.0, "intimacy": 0.0, "respect": 0.0,
                    "attraction": 0.0, "resentment": 0.0, "salience": 0.0,
                    "stage": p["stage"], "tick": int(envelope["tick"]),
                },
            )

    def all_edges(self, world_id: str) -> set[tuple[str, str]]:
        """세계의 전체 방향 엣지 (from, to) — 무결성 검사(kuzu_verify)의 실측값."""
        result = self._conn(world_id).execute(_ALL_EDGES)
        edges: set[tuple[str, str]] = set()
        while result.has_next():
            from_id, to_id = result.get_next()
            edges.add((from_id, to_id))
        return edges

    def player_graph(self, world_id: str, player_id: str) -> dict[str, Any]:
        """플레이어와 닿아 있는 엣지 전부 — FE 관계 그래프의 실측값 (방향별 max)."""
        result = self._conn(world_id).execute(_TOUCHING, {"id": player_id})
        edges: dict[str, dict[str, Any]] = {}
        while result.has_next():
            (a, b, trust, intimacy, respect, attraction,
             resentment, salience, stage, tick) = result.get_next()
            other = b if a == player_id else a
            score = strength(trust, intimacy, salience)
            current = edges.get(other)
            if current is None or score > current["strength"]:
                edges[other] = {
                    "actor_id": other,
                    "strength": score,
                    "stage": stage,
                    "dimensions": {
                        "trust": trust, "intimacy": intimacy, "respect": respect,
                        "attraction": attraction, "resentment": resentment,
                    },
                    "salience": salience,
                    "updated_tick": tick,
                }
        return {"player_id": player_id, "edges": sorted(
            edges.values(), key=lambda e: -e["strength"]
        )}

    def proximity(self, world_id: str, from_id: str, to_ids: list[str]) -> dict[str, float]:
        """관계 근접도 — 두 노드 사이 직접 엣지(양방향 max)의 strength (ADR-014 랭킹 항).

        다중 홉 근접도(공통 지인 경유)는 후속 — 직접 관계가 0이면 0이다.
        """
        conn = self._conn(world_id)
        scores: dict[str, float] = {}
        for to_id in to_ids:
            if to_id == from_id:
                scores[to_id] = 1.0
                continue
            result = conn.execute(_PAIR, {"x": from_id, "y": to_id})
            best = 0.0
            while result.has_next():
                trust, intimacy, salience = result.get_next()
                best = max(best, strength(trust, intimacy, salience))
            scores[to_id] = best
        return scores

    def tension_pairs(
        self, world_id: str, *, min_resentment: float = 0.1, limit: int = 5
    ) -> list[list[Any]]:
        """갈등 후보 — resentment 상위 방향 엣지 (Director의 개입 대상 질의, ADR-013).

        반환: [[from_id, to_id, resentment, trust], ...] resentment 내림차순.
        """
        result = self._conn(world_id).execute(
            _TENSION, {"min_resentment": min_resentment, "limit": limit}
        )
        pairs: list[list[Any]] = []
        while result.has_next():
            from_id, to_id, resentment, trust = result.get_next()
            pairs.append([from_id, to_id, round(resentment, 4), round(trust, 4)])
        return pairs

    def drop_world(self, world_id: str) -> None:
        """재구축용 파괴 (ADR-003 계약 3). Windows 파일 락 대비 짧은 재시도."""
        conn = self._conns.pop(world_id, None)
        if conn is not None:
            conn.close()
        db = self._dbs.pop(world_id, None)
        if db is not None:
            db.close()
        # kuzu 0.10+는 단일 파일 DB(+ .wal 사이드카)다 — 구버전 디렉터리 형식도 겸용
        target = self._base / world_id
        for attempt in range(5):
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                Path(f"{target}.wal").unlink(missing_ok=True)
                return
            except PermissionError:  # Windows: 핸들 해제가 close() 직후 지연될 수 있다
                time.sleep(0.1 * (attempt + 1))
        logger.warning("Kuzu DB 삭제 실패(잠김): %s — 다음 재구축에서 재시도", target)

    def drop_all(self) -> None:
        for world_id in list(self._dbs):
            self.drop_world(world_id)
        shutil.rmtree(self._base, ignore_errors=True)

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()
