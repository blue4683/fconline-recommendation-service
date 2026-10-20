"""명시한 합성 배치/시각의 stdio MCP. stdout은 프로토콜 전용이다."""

import argparse
from datetime import datetime

from FRS.agent.material import SyntheticLeagueSpec
from FRS.mcp.server import create_server


def main():
    parser = argparse.ArgumentParser(description="FRS 합성 stdio MCP")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--observed-at", required=True)
    arguments = parser.parse_args()
    try:
        spec = SyntheticLeagueSpec(
            run_id=arguments.run_id,
            observed_at=datetime.fromisoformat(arguments.observed_at),
            seed=123,
            members_per_family=3,
        )
    except ValueError:
        parser.error("유효한 run-id와 시간대 있는 observed-at을 명시해 주세요.")
    create_server(spec).run(transport="stdio")


if __name__ == "__main__":
    main()
