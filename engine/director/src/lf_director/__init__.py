"""lf-director — Director AI (ADR-013).

관찰(규칙: drama score, 침체 감지)은 매 tick, 개입은 임계·예산 안에서만.
발행 가능 이벤트는 world.* / system.director.* 뿐 (permissions.yaml에서 강제).
액터 직접 조작 금지 — 간접 개입 도구 화이트리스트가 전부다.

MVP(단계적 도입, ADR-013 §실행 모델): 개입 선택도 규칙 기반(rules.py).
LLM 개입 선택은 도파민 루프 검증 후 활성화된다.
실행 엔트리: python -m lf_director.main
"""

from lf_director.config import Config as Config
from lf_director.director import Director as Director
from lf_director.rules import BudgetState as BudgetState
from lf_director.rules import Intervention as Intervention
from lf_director.rules import decide as decide
from lf_director.signals import DramaWindow as DramaWindow
from lf_director.signals import drama_contribution as drama_contribution
