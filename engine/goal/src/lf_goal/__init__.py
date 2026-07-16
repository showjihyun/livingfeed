"""lf-goal — Goal Engine (ADR-012 §인지 루프 need/goal, docs/plan/04).

욕구 게이지(만족도) + 목표 진행 + congruence(정렬도). 전 과정 규칙 기반 —
행동이 섬기는 욕구를 채우고 걸린 목표를 진행시킨다. 리플레이 재현.

순수 로직만 담는다 (I/O 없음) — 상태 저장(Redis)과 이벤트 적재
(actor.goal.advanced)는 Actor Runtime의 goal 어댑터가 담당한다.
"""

from lf_goal.engine import (
    ACTION_NEED as ACTION_NEED,
)
from lf_goal.engine import (
    AppraisalResult as AppraisalResult,
)
from lf_goal.engine import (
    GoalAdvance as GoalAdvance,
)
from lf_goal.engine import (
    appraise_action as appraise_action,
)
from lf_goal.engine import (
    arc_focus_need as arc_focus_need,
)
from lf_goal.engine import (
    decay as decay,
)
from lf_goal.engine import (
    default_params as default_params,
)
from lf_goal.engine import (
    describe as describe,
)
from lf_goal.engine import (
    initial_state as initial_state,
)
from lf_goal.engine import (
    pressing_need as pressing_need,
)
from lf_goal.engine import (
    satisfy_from_interaction as satisfy_from_interaction,
)
from lf_goal.engine import (
    starvation as starvation,
)
from lf_goal.model import (
    NEEDS as NEEDS,
)
from lf_goal.model import (
    GoalState as GoalState,
)
