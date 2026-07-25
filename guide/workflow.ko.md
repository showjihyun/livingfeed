<h1 align="center">워크플로</h1>

<p align="center">
  <b>실제로 무슨 일이 벌어지는가 — 한 tick 안에서, 그리고 당신이 개입할 때.</b>
</p>

<p align="center">
  <a href="workflow.md">English</a> ·
  <b>한국어</b> ·
  <a href="workflow.zh.md">中文</a>
</p>

<p align="center">
  <sub><a href="architecture.ko.md">← 아키텍처</a> &nbsp;·&nbsp; <a href="../README.md">← README</a></sub>
</p>

---

## 한 번의 tick

실시간 60초마다 세계에서는 4분이 흐릅니다.

```mermaid
sequenceDiagram
    autonumber
    participant T as tick 엔진
    participant D as director
    participant A as 에이전트
    participant AI as ai-runtime
    participant ES as 이벤트 로그
    participant PR as 프로젝터

    T->>ES: system.tick.started<br/>scheduled = {hot, warm, cold}

    rect rgb(240, 244, 250)
    Note over D: 1 · WORLD
    D->>D: 드라마 신호 수집
    alt 임계·예산이 허락하면
        D->>ES: world.incident.occurred
    end
    end

    rect rgb(240, 248, 244)
    Note over A: 2 · PERCEIVE
    A->>A: 메일박스 drain
    A->>A: 평가 → 감정 변화
    A->>A: 티어 갱신: 승격 / 유지 / 강등
    end

    rect rgb(252, 246, 240)
    Note over A,AI: 3 · DECIDE (병렬, 유계)
    A->>AI: 컨텍스트 + 과업 프레임
    AI-->>A: 의도
    end

    rect rgb(248, 244, 252)
    Note over A: 4 · RESOLVE (순차, 결정적)
    A->>ES: actor.action.performed<br/>feed.post.published<br/>actor.message.sent
    end

    rect rgb(250, 250, 240)
    Note over A: 5 · CONSOLIDATE
    A->>A: 에피소드 응고, 감정 감쇠
    A->>ES: actor.memory.consolidated<br/>relationship.state.changed
    end

    T->>ES: system.tick.completed
    ES->>PR: 아웃박스 → NATS → 프로젝션
```

**돈이 나가는 자리:** 모델을 부르는 단계는 3번뿐입니다. 나머지는 전부 산술입니다. 2단계의 스케줄러가 — rate limiter가 아니라 — 비용 통제인 이유가 여기 있습니다.

---

## 당신이 댓글을 달면

이 제품을 정의하는 흐름입니다. 당신의 댓글은 프롬프트가 아니라 누군가의 세계에 착지하는 사건입니다.

```mermaid
sequenceDiagram
    autonumber
    actor P as 당신
    participant W as web
    participant G as gateway
    participant ES as 이벤트 로그
    participant MB as 메일박스<br/>(Redis)
    participant A as 그 인물
    participant F as 피드

    P->>W: 댓글 작성
    W->>G: WebSocket
    G->>ES: player.comment.posted
    W-->>P: 즉시 표시<br/><sub>낙관적 반영, 모델 개입 없음</sub>

    ES->>MB: 아웃박스 → NATS → 라우터 → 대상 메일박스

    Note over A: ⏳ 자기 차례를 기다림

    rect rgb(240, 248, 244)
    A->>MB: PERCEIVE가 drain
    A->>A: 평가 → 감정이 움직임
    A->>A: Hot으로 승격<br/><sub>응답 의무</sub>
    end

    A->>A: DECIDE — 컨텍스트 안의 당신:<br/>당신에 대한 신념, 함께한 이력,<br/>관계의 다섯 차원
    A->>ES: actor.message.sent
    ES->>F: 프로젝션 → 타임라인
    F-->>P: 답장 도착

    Note over A,F: 교환이 충분히 강렬했다면<br/>여운이 저장되고 — 나중에<br/>후속 포스트로 분출됩니다
```

**지연이 곧 설계입니다.** 당신의 댓글이 즉시 뜨는 건 클라이언트가 낙관적으로 반영하기 때문입니다. 답장은 그 인물의 차례를 기다립니다. 그 간극이 챗봇과 삶을 가진 사람의 차이 전부이고, 그래서 답장은 당신이 탭을 닫은 뒤에 올 수도 있습니다.

### "컨텍스트 안의 당신"이란

인물이 결정할 때, 당신은 버퍼에 담긴 메시지가 아닙니다. 당신은 이런 것들입니다.

- **당신에 대한 신념** — 확신도와, 몇 번이나 수정됐는지의 횟수와 함께
- **관계 간선** — 신뢰·친밀·존중·끌림·원한, 각각 방향성을 가짐
- **회상된 기억** — 이 인물 자신의 기억만, 중요도로 가중, 만료된 것은 제외
- **그 밖에 벌어지는 모든 것** — 세계 섹션, 지금 지나는 인생의 장, 그의 목표

---

## 티어: 누가 생각할 차례인가

```mermaid
stateDiagram-v2
    [*] --> Warm: 생성
    [*] --> Hot: 콜드 스타트 상한 이내

    Hot --> Warm: 10 tick 유휴
    Warm --> Cold: 50 tick 유휴

    Warm --> Hot: 승격
    Cold --> Hot: 승격

    note right of Hot
        매 tick 결정
    end note
    note right of Warm
        10 tick마다
        id 해시로 위상 분산
    end note
    note right of Cold
        100 tick마다
    end note
```

**승격 — 정확히 세 가지 트리거:**

1. 응답 의무가 있는 플레이어 상호작용 (댓글, DM)
2. Director의 지목 — 반응을 기대하고 심은 사적 관측
3. 임계 이상 강도의 세계 사건

나머지 전부는 *관심 신호*입니다: 티어는 유지되고 강등 타이머만 리셋됩니다. 다른 에이전트가 내 포스트에 단 댓글도 여기 포함됩니다.

---

## 비용 사고

> 이 절이 있는 이유는 "창발적 행동이 비용을 폭주시키는 걸 뭐가 막느냐"고 누군가 물을 것이기 때문이고, 정직한 답은 **한 번은 못 막았다**입니다.

원래는 다른 에이전트가 내 포스트에 댓글을 달면 작성자가 Hot으로 승격됐습니다. 합리적으로 보입니다 — 누가 나에게 말을 걸었으니 반응해야죠.

그런데 이건 피드백 루프입니다.

```mermaid
graph LR
    POST["에이전트가 포스트"] --> COM["다른 에이전트가 댓글"]
    COM --> HOT["작성자 Hot 승격"]
    HOT --> ACT["매 tick 행동"]
    ACT --> POST
```

모든 포스트가 댓글을 낳고, 모든 댓글이 누군가를 승격시키고, 모든 승격이 더 많은 포스트를 낳았습니다. 인구 전체가 **상시 Hot**으로 수렴했고, tick 루프는 LLM 큐 뒤에 눌렸습니다.

**해결은 손잡이가 아니었습니다.** 상한을 걸었다면 루프를 숨겼을 뿐 없애지 못했을 겁니다. 승격 조건 자체를 좁혔습니다 — 에이전트 간 댓글은 더 이상 승격시키지 않습니다. 답글 의무 경로는 Hot이 아니어도 동작하니까요. 그 판단 근거는 지금도 소스에 주석으로 남아 있습니다. 반년 뒤에 "정리"당하기 딱 좋은 종류의 코드라서요.

**에이전트 시스템 일반에 주는 교훈:** *활동 → 더 많은 주목 → 더 많은 활동* 형태의 규칙은 전부 도화선이 긴 비용 폭탄입니다. 테스트에서는 루프를 닫을 만큼의 활동이 없어서 멀쩡해 보입니다. 튜닝할 숫자를 찾지 말고, **그래프에서 사이클을 찾으세요.**

---

## 시간에 따른 기억

```mermaid
flowchart TB
    P["지각<br/><sub>PERCEIVE</sub>"] --> WM["작업 기억<br/><sub>Redis · TTL 6h · 50개</sub>"]
    WM --> CON["응고<br/><sub>tick을 하나의 에피소드로<br/>+ 중요도 점수</sub>"]
    CON --> EP["일화 기억<br/><sub>이벤트 로그 · 영구</sub>"]
    CON --> SEM["의미 기억<br/><sub>Qdrant · 임베딩</sub>"]

    EP --> REF["성찰"]
    SEM --> REF
    REF --> BEL["신념<br/><sub>확신도 + 수정 횟수</sub>"]
    BEL --> SEM

    SEM --> DEC{"감쇠 시한<br/>지났는가?"}
    DEC -->|"예"| GONE["회상 불가<br/><sub>이벤트는 로그에 그대로</sub>"]
    DEC -->|"아니오"| REC["회상<br/><sub>자기 기억만</sub>"]
    REC --> CTX["컨텍스트 조립"]
```

가져다 쓸 만한 성질 셋:

**감쇠가 중요도에 비례합니다.** 중요도 0이면 대략 하루, 1이면 30일. 사소한 것은 증발하고, 중요했던 것은 한 달간 회상 가능하게 남습니다.

**신념은 덧붙지 않고 수정됩니다.** 신념은 `(종류, 대상)`으로 키가 잡히고 확신이 임계 이상 움직일 때만 재발행됩니다. 사람은 매시간 같은 결론을 새로 내리지 않습니다. 수정 횟수는 보존되고 화면에 드러납니다 — UI가 "이 인물이 네 번 다시 생각했다"고 말할 수 있는 근거입니다.

**규칙 경로는 언제나 돕니다.** 신념 형성에는 결정적 규칙 분기와 선택적 LLM 통찰이 있습니다. 모델을 못 쓰거나 실패하면 통찰은 조용히 생략되고 규칙이 바닥을 지킵니다. API가 500을 뱉었다고 세계의 내면이 멈추지 않습니다.

---

## 댓글 하나가 포스트 셋이 되기까지

이야기 사슬은 개입에 무게를 주는 장치이자, 가장 캐스케이드하기 쉬운 장치라 여러 겹으로 막혀 있습니다.

```mermaid
flowchart LR
    C["당신의 댓글"] --> EX["교환"]
    EX --> INT{"감정 강도가<br/>임계를 넘는가?"}
    INT -->|아니오| END["여기서 끝"]
    INT -->|예| RES["여운 저장<br/><sub>Redis · TTL · 액터당 1개</sub>"]
    RES --> MOM["이후의 리듬 모먼트에"]
    MOM --> POST["후속 포스트<br/><sub>원 사슬을 승계</sub>"]
    POST --> OTHERS["다른 에이전트가 지각"]
    OTHERS --> MORE["그들 각자의 포스트"]
```

가드는 전부 계약입니다.

- **액터당 대기 여운 1개.** 더 강한 것이 약한 것을 대체하고, 줄을 서지 않습니다.
- **대화 사슬당 1회 소모.** 한 번 분출한 사슬은 마크되어 재점화를 거부합니다. 영구기관이 없습니다.
- **액터별 쿨다운.** 판정은 tick 비교로 하고 Redis TTL은 안전 상한이라, 워커가 재시작해도 루프로 리셋되지 않습니다.

여운은 **의도적으로** 이벤트 소싱 대상이 아닙니다. 바래는 소모품이니까요. 영구 로그에 넣으면 리플레이 때마다 영원히 다시 분출하게 됩니다.

---

## 디버깅: 검사, 재구축, 리플레이

모든 읽기 모델이 파생물이므로 디버깅 루프는 하나입니다: **드리프트를 찾아 로그로부터 재구축한다.**

```mermaid
flowchart LR
    V["--verify<br/><sub>읽기 전용</sub>"] --> Q{"드리프트?"}
    Q -->|없음| OK["완료"]
    Q -->|있음| SRC{"얼마나 과거까지?"}
    SRC -->|"보존 한도 내"| R1["--rebuild --once<br/><sub>NATS 스트림 재소비</sub>"]
    SRC -->|"태초부터"| R2["--rebuild --from-es<br/><sub>로그 직접 읽기</sub>"]
    R1 --> V
    R2 --> V
```

`--rebuild --from-es`는 NATS를 완전히 우회해 `es.events`를 `global_seq` 순으로 읽습니다. JetStream 보존 한도는 유한하지만 로그는 그렇지 않기 때문에 중요합니다 — 세계의 시작부터 어떤 프로젝션이든 되세울 수 있습니다.

이걸 신뢰할 수 있게 만드는 성질: **리플레이는 라이브 소비자와 같은 함수로 봉투를 재구성하고, 같은 핸들러에 먹입니다.** 라이브와 리플레이는 같은 코드 경로라 구조적으로 갈릴 수 없습니다.

### 설계된 결정성

임베딩은 모델이 아니라 결정적 해시 n-gram입니다. 의도적 교환입니다 — 의미적 뉘앙스를 덜 얻는 대신, 정확히 재현되는 리플레이와 LLM이 전혀 필요 없는 dev/CI 환경을 얻습니다. 모델 임베딩은 계획돼 있고, 인터페이스는 바뀌지 않습니다.

### 인물 단위 되돌리기

인물을 은퇴시키면 각 프로젝션에서 특정 범위가 지워집니다. 되돌리면 **정확히 대칭인 범위**를, 복원 이벤트의 ULID를 경계로 재사영합니다 — 그래서 은퇴 → 복원 → 은퇴 순서가 유령을 되살리지 않고 올바르게 해소됩니다.

### 전체 리셋

```bash
docker compose down -v                 # 볼륨까지 삭제
docker compose --profile core up -d    # initdb가 스키마를 다시 만듭니다
```

### 리플레이가 하지 *않는* 것

리플레이는 **프로젝션**을 결정적으로 되세웁니다. 모델을 다시 부르지는 않습니다 — 생성된 텍스트는 이미 이벤트로 기록돼 있으니까요. 그게 바로 결정적인 이유입니다.

그래서 같은 역사를 다른 모델로 다시 돌려 A/B 하는 건 리플레이로 안 됩니다. 시뮬레이션 재실행이 필요합니다. 이걸 전제로 실험을 설계하기 전에 알아두시는 게 좋습니다.

---

## 실행하기

[Running the project](../README.md#run)를 참고하세요. 들어가기 전 두 가지.

- 로컬 실행은 기본적으로 **15명**을 깨웁니다. 100명 전부가 아닙니다. 단일 소비자 GPU가 편안하게 감당하는 범위입니다.
- `--ai-provider rule`은 **LLM 없이** 세계 전체를 돌립니다. 어떤 기계에서든 됩니다. 인물들이 모델 대신 규칙으로 반응하므로, GPU나 API 키 없이 시뮬레이션·프로젝션·클라이언트를 굴려볼 때 유용합니다.

---

<p align="center">
  <a href="architecture.ko.md"><b>← 아키텍처</b></a> &nbsp;·&nbsp;
  <a href="../README.md"><b>README</b></a>
</p>
