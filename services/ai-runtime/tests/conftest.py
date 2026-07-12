import asyncio
import sys

# psycopg/NATS 비동기 테스트 공통 — Windows Selector 루프 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
