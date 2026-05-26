import os
import re
import math
import json
import email
import urllib.request
import urllib.error
from collections import Counter
from html.parser import HTMLParser

from flask import Flask, request, jsonify, Response

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAQ_PATH = os.path.join(BASE_DIR, "고객대응FAQ.doc")


def _load_dotenv() -> None:
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip("\"'")
            os.environ.setdefault(key.strip(), val)


_load_dotenv()

KANANA_BASE_URL = os.environ.get(
    "KANANA_BASE_URL",
    "https://kanana-o.a2s-endpoint.kr-central-2.kakaocloud.com/v1",
)
KANANA_API_KEY = os.environ.get("KANANA_API_KEY", "")
KANANA_MODEL = os.environ.get("KANANA_MODEL", "kanana-o")

RETRIEVAL_TOP_K = 5

# ──────────────────────────────────────────────
# 1. MHTML → Plain Text
# ──────────────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "td", "th"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._skip == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts).replace("\xa0", " ")
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _load_faq_text() -> str:
    with open(FAQ_PATH, "rb") as f:
        msg = email.message_from_bytes(f.read())
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode("utf-8", errors="replace")
            ext = _HTMLTextExtractor()
            ext.feed(html)
            return ext.text()
    raise RuntimeError("FAQ 문서에서 HTML 본문을 찾을 수 없습니다.")


# ──────────────────────────────────────────────
# 2. FAQ Chunker
# ──────────────────────────────────────────────

def _chunk_by_faq_number(text: str) -> list[str]:
    items = re.split(r"\n(?=\d{1,3}\.\s*\[)", text)
    items = [i.strip() for i in items if i.strip()]
    if len(items) < 5:
        return _fixed_chunks(text)
    out = []
    for item in items:
        if len(item) > 1500:
            out.extend(_fixed_chunks(item, 800, 150))
        else:
            out.append(item)
    return out


def _fixed_chunks(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 > size and cur:
            chunks.append(cur)
            words = cur.split()
            tail = " ".join(words[-(overlap // 4):]) if len(words) > overlap // 4 else ""
            cur = (tail + "\n" + p) if tail else p
        else:
            cur = (cur + "\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks


# ──────────────────────────────────────────────
# 3. BM25 Retriever (zero dependency)
# ──────────────────────────────────────────────

class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self._df: list[Counter] = []
        self._dl: list[int] = []
        self._idf: dict[str, float] = {}
        self._avgdl = 0.0
        self._build()

    @staticmethod
    def _tok(text: str) -> list[str]:
        t = text.lower()
        words = t.split()
        bigrams = [w[i:i + 2] for w in words for i in range(len(w) - 1)]
        return words + bigrams

    def _build(self):
        df: Counter = Counter()
        for doc in self.docs:
            toks = self._tok(doc)
            self._dl.append(len(toks))
            freq = Counter(toks)
            self._df.append(freq)
            for t in set(toks):
                df[t] += 1
        n = len(self.docs)
        self._avgdl = sum(self._dl) / n if n else 1.0
        for t, f in df.items():
            self._idf[t] = math.log((n - f + 0.5) / (f + 0.5) + 1.0)

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        qtoks = self._tok(query)
        scores = []
        for idx, freq in enumerate(self._df):
            s = 0.0
            dl = self._dl[idx]
            for t in qtoks:
                if t not in freq:
                    continue
                tf = freq[t]
                idf = self._idf.get(t, 0.0)
                s += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            scores.append((s, idx))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [(self.docs[i], sc) for sc, i in scores[:k] if sc > 0]


# ──────────────────────────────────────────────
# 4. Initialise index at startup
# ──────────────────────────────────────────────

print("[*] FAQ 문서 로딩 중...")
_faq_text = _load_faq_text()
_chunks = _chunk_by_faq_number(_faq_text)
_retriever = BM25(_chunks)
print(f"[+] 인덱싱 완료: {len(_chunks)}개 청크")

# ──────────────────────────────────────────────
# 5. Kanana-o LLM (direct HTTP, no openai lib)
# ──────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
다음은 사내 고객대응 FAQ 문서에서 발췌한 내용입니다. 이 문서 내용만 사용해서 질문에 답변하세요.
문서에 없는 내용은 추측하지 말고 "FAQ에서 확인되지 않습니다. 담당자에게 문의해 주세요."라고 안내하세요.
접속 경로, 메뉴, URL 등 구체적 절차를 포함해서 답변하세요.

--- 문서 1 ---
{doc1}

--- 문서 2 ---
{doc2}

--- 문서 3 ---
{doc3}

질문: {question}
답변:"""


def _call_kanana(question: str, context: list[str], history: list[dict]) -> str:
    docs = context[:3] + ["(없음)"] * (3 - len(context))
    prompt = _PROMPT_TEMPLATE.format(
        doc1=docs[0], doc2=docs[1], doc3=docs[2], question=question
    )
    messages = []
    for m in history[-4:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": prompt})

    body = json.dumps({"model": KANANA_MODEL, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        f"{KANANA_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {KANANA_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {}
        if code == 429:
            return "⚠️ 일일 API 쿼터(10회)를 모두 소진하였습니다. 내일 00시에 초기화됩니다."
        if code == 401:
            return "⚠️ API 키가 유효하지 않습니다. 설정을 확인해 주세요."
        if code == 500:
            return "⚠️ GPU 서버 요청 폭주로 처리가 지연되고 있습니다. 잠시 후 다시 시도해 주세요."
        return f"⚠️ API 오류 ({code}): {detail.get('error', e.reason)}"
    except Exception as e:
        return f"⚠️ API 호출 중 오류 발생: {e}"


# ──────────────────────────────────────────────
# 6. Flask App
# ──────────────────────────────────────────────

app = Flask(__name__)


_HTML_TEMPLATE = open(
    os.path.join(BASE_DIR, "templates", "index.html"), encoding="utf-8"
).read()


@app.route("/")
def index():
    html = _HTML_TEMPLATE.replace("{{ chunk_count }}", str(len(_chunks)))
    return Response(html, content_type="text/html; charset=utf-8")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    question = (data.get("question") or "").strip()
    history = data.get("history", [])

    if not question:
        return jsonify({"error": "질문을 입력해 주세요."}), 400

    results = _retriever.search(question, k=RETRIEVAL_TOP_K)
    context_docs = [doc for doc, _ in results]
    refs = [
        {"text": doc[:300] + ("..." if len(doc) > 300 else ""), "score": round(sc, 2)}
        for doc, sc in results
    ]

    answer = _call_kanana(question, context_docs, history)

    is_error = answer.startswith("⚠️")
    if is_error and context_docs:
        fallback = "🔍 API를 사용할 수 없어 FAQ 검색 결과를 직접 보여드립니다.\n\n"
        for i, doc in enumerate(context_docs[:3], 1):
            fallback += f"━━ 검색 결과 {i} ━━\n{doc}\n\n"
        answer = answer + "\n\n" + fallback

    return jsonify({"answer": answer, "references": refs})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"[*] http://localhost:{port} 에서 챗봇을 시작합니다.")
    app.run(host="127.0.0.1", port=port, debug=False)
