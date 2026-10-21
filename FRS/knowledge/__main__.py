"""기본은 검색 계획만 출력한다. --run과 --upload-trace는 별도 명시 동작이다."""

import argparse
import json
import re
from pathlib import Path

from FRS.knowledge.api import build_local_service
from FRS.knowledge.retrieval import load_index
from FRS.knowledge.tracing import TraceSettings, export_trace, preview_trace
from FRS.schema.scope import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--upload-trace", action="store_true")
    args = parser.parse_args()
    if args.upload_trace and not args.run:
        parser.error("--upload-trace는 --run과 함께 명시해야 합니다.")
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    index = load_index()
    hits = index.search(args.question)
    if not args.run:
        print(
            json.dumps(
                dict(
                    mode="retrieval_plan_only",
                    corpus_version=index.version,
                    model_requests=0,
                    trace_requests=0,
                    hits=[
                        dict(source_id=h.passage.source_id, score=h.score) for h in hits
                    ],
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not args.run_id or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", args.run_id):
        parser.error("새 실행 ID를 명시해야 합니다.")
    # 설정을 먼저 확인하고 기존 실행을 덮어쓰지 않는다.
    trace_settings = TraceSettings.from_env() if args.upload_trace else None
    directory = ROOT / "artifacts/knowledge-runs" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "started.json").write_text(
        json.dumps(
            dict(
                run_id=args.run_id,
                metadata_limit=2,
                inference_limit=1,
                trace_limit=4 if args.upload_trace else 0,
            )
        )
    )
    try:
        service = build_local_service()
        result, observation = service.ask(args.question)
        record = observation.to_dict()
        data = dict(
            result=result.to_dict(),
            observation=record,
            trace_preview=preview_trace(record),
        )
        (directory / "result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2)
        )
        if args.upload_trace:
            data["trace_export"] = export_trace(
                record, enabled=True, settings=trace_settings
            )
            (directory / "result.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2)
            )
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        (directory / "stopped.json").write_text(
            '{"status":"stopped","automatic_retry":false}'
        )
        parser.exit(1, "지식 Q&A 실행 중단. 기존 실행은 재사용하지 않습니다.\n")


if __name__ == "__main__":
    main()
