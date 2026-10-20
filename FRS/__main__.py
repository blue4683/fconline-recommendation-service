import argparse
import importlib


def main(argv=None):
    parser = argparse.ArgumentParser(description="FC온라인 추천 서비스 개발 도구")
    parser.add_argument(
        "--check-environment",
        action="store_true",
        help="수집·DB 접속·학습 없이 기본 모듈 import를 확인합니다.",
    )
    args = parser.parse_args(argv)
    if args.check_environment:
        for module in (
            "FRS.schema.models",
            "FRS.schema.scope",
            "FRS.utils.crawling.get_player_data",
            "FRS.utils.db.save_data",
            "FRS.core.position_recommend.model",
            "FRS.core.squad_recommend.recommend",
            "FRS.agent.graph",
        ):
            importlib.import_module(module)
        print("기본 모듈 import 확인 완료 (수집·DB 접속·학습 미실행)")
        return

    from FRS.core.squad_recommend.recommend import recommend

    recommend()


if __name__ == "__main__":
    main()
