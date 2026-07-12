"""lf-relationship — Relationship Engine (ADR-016).

A→B 방향별 독립 5차원(trust/intimacy/respect/attraction/resentment) + stage.
stage 전이는 수치가 아니라 이벤트(행동)로만. 관계는 sparse — 상호작용 시 생성,
액터당 활성 상한 150 (Dunbar). 갱신은 규칙 기반 (ADR-015와 동일 논거).

이 패키지는 순수 로직만 담는다 (I/O 없음) — 상태 저장(Redis)과
relationship.* 이벤트 적재는 Actor Runtime의 relationship 어댑터가 담당한다.
"""

from lf_relationship.engine import (
    UpdateResult as UpdateResult,
)
from lf_relationship.engine import (
    apply_interaction as apply_interaction,
)
from lf_relationship.engine import (
    consolidate_emotion as consolidate_emotion,
)
from lf_relationship.engine import (
    consume_pending as consume_pending,
)
from lf_relationship.engine import (
    decay as decay,
)
from lf_relationship.engine import (
    default_params as default_params,
)
from lf_relationship.engine import (
    transition_stage as transition_stage,
)
from lf_relationship.model import (
    DIMENSIONS as DIMENSIONS,
)
from lf_relationship.model import (
    STAGES as STAGES,
)
from lf_relationship.model import (
    RelationshipState as RelationshipState,
)
