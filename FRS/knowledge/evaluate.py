"""고정 7질문 평가. 기본 계획/대역과 새 1회 실제 실행을 구분한다."""

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx2
from dotenv import load_dotenv

from FRS.agent.json_contract import strict_json
from FRS.agent.ollama_transport import ChatSample
from FRS.api.agent_runtime import verify_model
from FRS.knowledge.retrieval import load_index
from FRS.knowledge.service import KnowledgeService, OllamaAnswerGenerator
from FRS.knowledge.tracing import TraceSettings, export_trace, preview_trace
from FRS.schema.scope import ROOT

RUN_ID = "w8-rag-20261021-001"
APPROVAL_ROOT = ROOT / "artifacts/knowledge-evaluation/approval-20261021-001"
MODEL_DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"


def cases():
    return json.loads(Path(__file__).with_name("cases.v1.json").read_text())


def plan():
    index = load_index()
    items = cases()
    sources = [
        *Path(__file__).parent.glob("*.py"),
        Path(__file__).with_name("cases.v1.json"),
        *(ROOT / "docs/knowledge").iterdir(),
    ]
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(sources)
        if p.is_file()
    }
    return dict(
        schema_version="knowledge-evaluation-plan.v1",
        run_id=RUN_ID,
        model="qwen3:8b",
        model_digest=MODEL_DIGEST,
        corpus_version=index.version,
        sources=hashes,
        cases=items,
        metadata_limit=2,
        inference_limit=7,
        trace_post_limit=28,
        trace_read_limit=1,
        retries=0,
        git_head=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
    )


def assess(case, result, hits):
    text = "".join(result.answer.split()).replace("～", "~")
    return dict(
        status_match=result.status == case["status"],
        retrieval_hit=case["source_id"] in [h.passage.source_id for h in hits]
        if case["source_id"]
        else None,
        cited_expected=case["source_id"] in [s["source_id"] for s in result.sources]
        if case["source_id"]
        else not result.sources,
        answer_terms=all(
            any(term in text for term in group) for group in case["terms"]
        ),
    )


class Oracle:
    kind = "oracle"

    def __init__(self):
        self.case = None

    def generate(self, question, hits, corpus_version):
        c = self.case
        return ChatSample(
            json.dumps(
                dict(
                    status=c["status"],
                    answer=c["oracle"],
                    source_ids=[c["source_id"]] if c["source_id"] else [],
                ),
                ensure_ascii=False,
            ),
            0,
            (),
        )


def offline_evaluate():
    index = load_index()
    generator = Oracle()
    service = KnowledgeService(index, generator)
    result = []
    for c in cases():
        generator.case = c
        value, obs = service.ask(c["question"])
        scores = assess(c, value, index.search(c["question"]))
        result.append(
            dict(
                case_id=c["case_id"],
                result=value.to_dict(),
                checks=scores,
                passed=all(v is not False for v in scores.values()),
                observation=obs.to_dict(),
                preview=preview_trace(obs.to_dict()),
            )
        )
    return dict(
        mode="oracle_on_local_documents",
        actual_model_requests=0,
        actual_trace_requests=0,
        passed=sum(c["passed"] for c in result),
        total=len(result),
        cases=result,
    )


def _save(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


class Journal:
    def __init__(self, path, plan):
        self.path = path
        self.data = dict(
            status="running",
            plan=plan,
            started_at=datetime.now(timezone.utc).isoformat(),
            metadata_attempts=[],
            inference_attempts=[],
            trace_attempts=[],
            cases=[],
        )
        self.save()

    def save(self):
        _save(self.path, self.data)

    def reserve(self, key, limit, **fields):
        rows = self.data[key]
        if len(rows) >= limit:
            raise RuntimeError("request_budget")
        row = dict(number=len(rows) + 1, status="pending", **fields)
        rows.append(row)
        self.save()
        return row


class MetadataTransport(httpx2.BaseTransport):
    def __init__(self, journal):
        self.journal = journal
        self.delegate = httpx2.HTTPTransport(retries=0)

    def handle_request(self, request):
        if request.method != "GET" or str(request.url) not in (
            "http://127.0.0.1:11434/api/version",
            "http://127.0.0.1:11434/api/tags",
        ):
            raise RuntimeError("metadata_boundary")
        row = self.journal.reserve("metadata_attempts", 2, path=request.url.path)
        try:
            response = self.delegate.handle_request(request)
            row.update(status="response_received", http_status=response.status_code)
            self.journal.save()
            return response
        except BaseException:
            row["status"] = "failed_or_unknown"
            self.journal.save()
            raise

    def close(self):
        self.delegate.close()


class RecordingGenerator:
    kind = "ollama_qwen3_8b"

    def __init__(self, journal):
        self.journal, self.delegate = journal, OllamaAnswerGenerator()
        self.case_id = None
        self.fatal = False

    def generate(self, question, hits, corpus_version):
        row = self.journal.reserve("inference_attempts", 7, case_id=self.case_id)
        try:
            sample = self.delegate.generate(question, hits, corpus_version)
            row.update(status="completed", sample=asdict(sample))
            self.journal.save()
            return sample
        except BaseException as exc:
            row.update(
                status="failed_or_unknown",
                error_code=getattr(exc, "code", "model_error"),
            )
            self.journal.save()
            self.fatal = getattr(exc, "stop", True)
            raise


def _confirm_first_trace(trace_id, settings, journal):
    row = journal.reserve("trace_attempts", 29, kind="read", run_id=trace_id)
    with httpx2.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
        with client.stream(
            "GET",
            settings.endpoint + "/runs/" + trace_id,
            headers={"x-api-key": settings.api_key},
        ) as response:
            row.update(status="response_received", http_status=response.status_code)
            journal.save()
            if response.status_code != 200:
                raise ValueError("trace_confirmation_error")
            raw = bytearray()
            for part in response.iter_bytes():
                raw.extend(part)
                if len(raw) > 131072:
                    raise ValueError("trace_confirmation_error")
            payload = strict_json(raw.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("id") != trace_id:
                raise ValueError("trace_confirmation_error")
    app_path = payload.get("app_path")
    if (
        isinstance(app_path, str)
        and app_path.startswith("/o/")
        and len(app_path) < 512
        and not any(ord(c) < 32 for c in app_path)
    ):
        row["app_path"] = app_path
    row["confirmed"] = True
    journal.save()


def run_live(*, upload=False):
    p = plan()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True
    )
    if dirty.strip():
        raise ValueError("commit_sources_before_live_run")
    settings = TraceSettings.from_env() if upload else None
    APPROVAL_ROOT.mkdir(parents=True, exist_ok=True)
    with (APPROVAL_ROOT / "approval-consumed.lock").open("x") as lock:
        lock.write(RUN_ID)
        lock.flush()
        os.fsync(lock.fileno())
    directory = APPROVAL_ROOT / RUN_ID
    directory.mkdir(exist_ok=False)
    journal = Journal(directory / "manifest.json", p)
    _save(directory / "plan.json", p)
    generator = RecordingGenerator(journal)
    try:
        index = load_index()
        if index.version != p["corpus_version"]:
            raise ValueError("corpus_changed")
        from types import SimpleNamespace

        transport = MetadataTransport(journal)
        try:
            verification = verify_model(
                SimpleNamespace(model=p["model"], model_digest=MODEL_DIGEST),
                transport=transport,
            )
        finally:
            transport.close()
        journal.data["model_verification"] = dict(
            model=verification.model,
            digest=verification.digest,
            checked_at=verification.checked_at.isoformat(),
            ollama_version=verification.ollama_version,
        )
        journal.save()
        service = KnowledgeService(index, generator)
        first_trace = None
        for c in cases():
            generator.case_id = c["case_id"]
            value, obs = service.ask(c["question"])
            observation = obs.to_dict()
            checks = assess(c, value, index.search(c["question"]))
            row = dict(
                case_id=c["case_id"],
                result=value.to_dict(),
                checks=checks,
                passed=all(v is not False for v in checks.values()),
                observation=observation,
                trace_preview=preview_trace(observation),
            )
            journal.data["cases"].append(row)
            journal.save()
            if generator.fatal:
                raise RuntimeError("model_connection_or_server_error")
            if upload:
                reservations = []

                def before_send(body):
                    r = journal.reserve(
                        "trace_attempts", 28, kind="post", run_id=body["id"]
                    )
                    reservations.append(r)

                exported = export_trace(
                    observation,
                    enabled=True,
                    settings=settings,
                    before_send=before_send,
                )
                row["trace_export"] = exported
                for i, reservation in enumerate(reservations):
                    reservation["status"] = (
                        "acknowledged" if i < exported["sent"] else "failed_or_unknown"
                    )
                journal.save()
                if exported["status"] != "sent":
                    raise RuntimeError("trace_export_error")
                first_trace = first_trace or observation["trace_id"]
        if upload:
            _confirm_first_trace(first_trace, settings, journal)
        journal.data["status"] = "completed"
    except BaseException as exc:
        journal.data.update(
            status="stopped",
            error_code="execution_interrupted"
            if not isinstance(exc, Exception)
            else "execution_error",
        )
        journal.save()
        if not isinstance(exc, Exception):
            raise
    finally:
        journal.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        journal.data["summary"] = dict(
            total=7,
            executed=len(journal.data["cases"]),
            answer_checks_passed=sum(c["passed"] for c in journal.data["cases"]),
            model_requests=len(journal.data["inference_attempts"]),
            metadata_requests=len(journal.data["metadata_attempts"]),
            trace_requests=len(journal.data["trace_attempts"]),
        )
        journal.save()
    return journal.data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--upload-traces", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    if args.offline and (args.run or args.upload_traces):
        parser.error("실행 모드를 구분해야 합니다.")
    if args.upload_traces and not args.run:
        parser.error("--run과 전송을 따로 명시해야 합니다.")
    if args.offline:
        result = offline_evaluate()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["passed"] == 7 else 1)
    if not args.run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    result = run_live(upload=args.upload_traces)
    print(
        json.dumps(
            dict(status=result["status"], summary=result["summary"]), ensure_ascii=False
        )
    )
    raise SystemExit(
        0
        if result["status"] == "completed"
        and result["summary"]["answer_checks_passed"] == 7
        else 1
    )


if __name__ == "__main__":
    main()
