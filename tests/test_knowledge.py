import json
import shutil

import pytest

from FRS.agent.ollama_transport import ChatSample
from FRS.knowledge.retrieval import CORPUS_FILES, KnowledgeIndex, Passage, load_index
from FRS.knowledge.service import KnowledgeService, generation_request
from FRS.schema.scope import ROOT


class Generator:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []

    def generate(self, question, hits, corpus_version):
        self.calls.append((question, hits, corpus_version))
        if self.error:
            raise self.error
        content = self.content or json.dumps(
            dict(
                status="answered",
                answer=hits[0].passage.text,
                source_ids=[hits[0].passage.source_id],
            )
        )
        return ChatSample(content, 0.01, (("prompt_eval_count", 12), ("eval_count", 8)))


@pytest.mark.parametrize(
    "question,expected",
    [
        ("지원 포메이션은?", "support/supported-formations"),
        ("5강과 8강 차이는?", "reinforcement/five-to-eight"),
        ("추천에 필요한 필수 조건은?", "support/required-conditions"),
        (
            "같은 인물의 시즌 카드를 중복으로 쓸 수 있나요?",
            "verification/squad-constraints",
        ),
        ("강화 보너스로 가격 급여를 계산하나요?", "reinforcement/not-price-or-salary"),
        ("성능 점수와 OVR은?", "verification/score-and-optimality"),
        ("데모는 합성 자료인가요?", "verification/synthetic-scope"),
        (
            "파생 계산과 추천 허용 강화 범위?",
            "reinforcement/derivation-and-recommendation",
        ),
    ],
)
def test_known_questions_find_relevant_passages(question, expected):
    index = load_index()
    hits = index.search(question)
    assert expected in [h.passage.source_id for h in hits]
    assert len(hits) <= 3
    assert all(h.score > 0 for h in hits)
    assert hits == index.search(question)


def test_default_corpus_version_and_foreign_files_are_not_loaded(tmp_path):
    for name in (*CORPUS_FILES, "manifest.v1.json"):
        shutil.copy(ROOT / "docs/knowledge" / name, tmp_path / name)
    (tmp_path / "secrets.md").write_text("not searchable secret")
    assert load_index(tmp_path).version == load_index().version
    (tmp_path / CORPUS_FILES[0]).write_text("changed")
    with pytest.raises(ValueError, match="해시"):
        load_index(tmp_path)


def test_manifest_and_document_symlink_are_rejected(tmp_path):
    for name in (*CORPUS_FILES, "manifest.v1.json"):
        shutil.copy(ROOT / "docs/knowledge" / name, tmp_path / name)
    (tmp_path / CORPUS_FILES[0]).unlink()
    (tmp_path / CORPUS_FILES[0]).symlink_to(ROOT / "docs/knowledge" / CORPUS_FILES[0])
    with pytest.raises(ValueError):
        load_index(tmp_path)
    (tmp_path / "manifest.v1.json").write_text("{}")
    with pytest.raises(ValueError):
        load_index(tmp_path)


@pytest.mark.parametrize("question", [None, 1, True, "", "   ", "가" * 4001])
def test_invalid_question_does_not_call_generator(question):
    generator = Generator()
    service = KnowledgeService(load_index(), generator)
    with pytest.raises(ValueError):
        service.ask(question)
    assert generator.calls == []


def test_no_hits_does_not_generate_and_records_insufficient_evidence():
    generator = Generator()
    service = KnowledgeService(load_index(), generator)
    result, observation = service.ask("zzzzzzzz")
    assert result.status == "insufficient_evidence" and not result.sources
    assert generator.calls == []
    assert [s["stage"] for s in observation.to_dict()["spans"]] == ["retrieve"]


def test_answer_has_exact_retrieved_source_and_separate_observation():
    generator = Generator()
    service = KnowledgeService(load_index(), generator)
    result, observation = service.ask("5강과 8강 차이는?")
    assert result.status == "answered"
    assert result.sources[0]["source_id"] == "reinforcement/five-to-eight"
    assert "9" in result.answer
    assert result.corpus_version == service.index.version
    trace = observation.to_dict()
    assert [s["stage"] for s in trace["spans"]] == ["retrieve", "generate", "validate"]
    assert trace["spans"][1]["input_tokens"] == 12
    assert trace["spans"][1]["output_tokens"] == 8
    assert "question" not in json.dumps(trace) and "answer" not in json.dumps(trace)


@pytest.mark.parametrize(
    "content",
    [
        "{bad",
        '{"status":"answered","status":"answered"}',
        json.dumps(
            dict(
                status="answered", answer="근거 없는 답", source_ids=["support/missing"]
            )
        ),
        json.dumps(dict(status="answered", answer="", source_ids=[])),
        json.dumps(
            dict(
                status="answered",
                answer="a",
                source_ids=["reinforcement/five-to-eight"] * 2,
            )
        ),
        json.dumps(dict(status="insufficient_evidence", answer="추측", source_ids=[])),
        json.dumps(
            dict(
                status="answered",
                answer="a" * 1201,
                source_ids=["reinforcement/five-to-eight"],
            )
        ),
        json.dumps(
            dict(
                status="answered",
                answer="a",
                source_ids=["reinforcement/five-to-eight"],
                extra="bad",
            )
        ),
    ],
)
def test_invalid_model_output_returns_no_answer_or_citations(content):
    service = KnowledgeService(load_index(), Generator(content))
    result, observation = service.ask("5강과 8강 차이는?")
    assert result.status == "provider_error" and result.error_code == "invalid_output"
    assert result.answer == "" and not result.sources
    assert observation.to_dict()["status"] == "invalid_output"


def test_existing_corpus_source_not_retrieved_is_rejected():
    index = load_index()
    hits = index.search("5강과 8강 차이는?")
    other = next(
        p.source_id
        for p in index.passages
        if p.source_id not in [h.passage.source_id for h in hits]
    )
    content = json.dumps(dict(status="answered", answer="a", source_ids=[other]))
    assert (
        KnowledgeService(index, Generator(content))
        .ask("5강과 8강 차이는?")[0]
        .error_code
        == "invalid_output"
    )


def test_model_insufficient_evidence_has_no_generated_claim():
    generator = Generator(
        json.dumps(dict(status="insufficient_evidence", answer="", source_ids=[]))
    )
    result, obs = KnowledgeService(load_index(), generator).ask("지원 포메이션은?")
    assert (
        result.status == "insufficient_evidence"
        and result.answer == ""
        and not result.sources
    )
    assert obs.to_dict()["status"] == "insufficient_evidence"


def test_model_failure_is_single_attempt_and_error_message_is_safe():
    generator = Generator(error=RuntimeError("secret exception path"))
    result, obs = KnowledgeService(load_index(), generator).ask("지원 포메이션은?")
    assert len(generator.calls) == 1 and result.error_code == "model_error"
    assert "secret" not in json.dumps(result.to_dict()) + json.dumps(obs.to_dict())


def test_cancellation_does_not_start_generation():
    generator = Generator()
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        KnowledgeService(load_index(), generator).ask("지원 포메이션은?", check=check)
    assert not generator.calls


def test_generation_request_binds_exact_sources_without_external_search():
    index = load_index()
    hits = index.search("지원 포메이션은?")
    data = generation_request("지원 포메이션은?", hits, index.version)
    assert (
        data["model"] == "qwen3:8b" and data["keep_alive"] == 0 and not data["stream"]
    )
    assert data["format"]["properties"]["source_ids"]["items"]["enum"] == [
        h.passage.source_id for h in hits
    ]
    content = json.loads(data["messages"][1]["content"])
    assert content["corpus_version"] == index.version
    assert content["sources"][0]["text"] == hits[0].passage.text


@pytest.mark.parametrize(
    "passages",
    [
        [],
        [Passage("bad", "f", "t", "text")],
        [Passage("a/b", "f", "t", "text"), Passage("a/b", "f", "t", "different")],
        [Passage("a/b", "f", "t", "a" * 1201)],
    ],
)
def test_invalid_corpus_rejected(passages):
    with pytest.raises(ValueError):
        KnowledgeIndex(passages)
