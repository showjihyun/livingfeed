"""lf-emotion — Emotion Engine (ADR-015).

2층 구조: mood(PAD 3차원, 느림) + emotion 인스턴스(대상·출처 있음, 빠름).
전 과정 규칙 기반 — LLM은 감정 계산에 관여하지 않는다 (리플레이 재현성).
파라미터는 params.yaml 단일 파일로 관리한다.

이 패키지는 순수 로직만 담는다 (I/O 없음) — 상태 저장(Redis)과 이벤트
적재(actor.emotion.shifted)는 Actor Runtime의 emotion 어댑터가 담당한다.
"""

from lf_emotion.engine import (
    AppraisalResult as AppraisalResult,
)
from lf_emotion.engine import (
    appraise_goal as appraise_goal,
)
from lf_emotion.engine import (
    appraise_interaction as appraise_interaction,
)
from lf_emotion.engine import (
    appraise_post as appraise_post,
)
from lf_emotion.engine import (
    decay as decay,
)
from lf_emotion.engine import (
    default_params as default_params,
)
from lf_emotion.engine import (
    describe as describe,
)
from lf_emotion.model import (
    EmotionInstance as EmotionInstance,
)
from lf_emotion.model import (
    EmotionState as EmotionState,
)
from lf_emotion.model import (
    Pad as Pad,
)
from lf_emotion.model import (
    baseline_from_ocean as baseline_from_ocean,
)
