"""스캐폴드 단계: relay 루프는 로드맵 5단계(Core Engine)에서 구현된다 (ADR-017)."""

import asyncio


async def run() -> None:
    raise NotImplementedError("outbox relay는 Core Engine 단계에서 구현된다 (ADR-017 §1)")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
