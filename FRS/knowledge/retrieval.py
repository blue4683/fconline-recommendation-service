"""고정 허용 문서의 제목/구간·해시를 보존하는 로컬 검색."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from FRS.schema.scope import ROOT

CORPUS_FILES = ("support.md", "reinforcement.md", "verification.md")


@dataclass(frozen=True)
class Passage:
    source_id: str
    document: str
    title: str
    text: str


@dataclass(frozen=True)
class Hit:
    passage: Passage
    score: float


class KnowledgeIndex:
    def __init__(self, passages):
        self.passages = tuple(passages)
        if not self.passages or any(type(p) is not Passage for p in self.passages):
            raise ValueError("검증 가능한 지식 구간이 필요합니다.")
        ids = [p.source_id for p in self.passages]
        if len(ids) != len(set(ids)) or any(
            not re.fullmatch(r"[a-z-]+/[a-z-]+", p.source_id)
            or not p.text.strip()
            or len(p.text) > 1200
            for p in self.passages
        ):
            raise ValueError("지식 구간 ID/본문이 잘못됐습니다.")
        payload = json.dumps(
            [asdict(p) for p in self.passages], ensure_ascii=False, sort_keys=True
        )
        self.version = hashlib.sha256(payload.encode()).hexdigest()
        self._vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4))
        self._matrix = self._vectorizer.fit_transform(
            p.title + "\n" + p.text for p in self.passages
        )

    def search(self, question):
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > 4000
        ):
            raise ValueError("질문은 1~4000자여야 합니다.")
        scores = cosine_similarity(
            self._vectorizer.transform([question]), self._matrix
        )[0]
        order = sorted(
            range(len(scores)), key=lambda i: (-scores[i], self.passages[i].source_id)
        )
        return tuple(
            Hit(self.passages[i], float(scores[i])) for i in order[:3] if scores[i] > 0
        )


def load_index(directory=None):
    """임의 경로/파일 자동 탐색 없이 세 문서만 읽는다."""
    base = Path(directory) if directory is not None else ROOT / "docs/knowledge"
    manifest = json.loads((base / "manifest.v1.json").read_text())
    if (
        set(manifest) != {"schema_version", "documents", "corpus_version"}
        or manifest["schema_version"] != "knowledge-corpus.v1"
        or set(manifest["documents"]) != set(CORPUS_FILES)
    ):
        raise ValueError("지식 문서 manifest가 잘못됐습니다.")
    passages = []
    for filename in CORPUS_FILES:
        source = base / filename
        if (
            source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != manifest["documents"][filename]
        ):
            raise ValueError("지식 문서 해시가 다릅니다.")
        raw = source.read_text(encoding="utf-8")
        if len(raw) > 16000:
            raise ValueError("지식 문서가 너무 큽니다.")
        for match in re.finditer(r"(?m)^## ([a-z-]+)\n", raw):
            start = match.end()
            end = raw.find("\n## ", start)
            text = raw[start : end if end >= 0 else len(raw)].strip()
            passages.append(
                Passage(filename[:-3] + "/" + match[1], filename, match[1], text)
            )
    index = KnowledgeIndex(passages)
    if index.version != manifest["corpus_version"]:
        raise ValueError("지식 corpus 버전이 다릅니다.")
    return index
