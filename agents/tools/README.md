# agents/tools — 액터 MCP 도구

액터가 세계와 상호작용하는 도구(장소 이동, 아이템 사용, 발화 등)를 MCP 서버로 정의한다.
AI Runtime이 tool-use 루프를 관리하고, 도구 실행 결과는 전부 이벤트로 기록된다 (ADR-018 §6).

Core Engine 단계(로드맵 5)에서 첫 도구 세트가 추가된다.
