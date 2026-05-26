import os
import re
import math
import json
import time
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
    """로컬 개발용. Vercel은 대시보드 Environment Variables를 사용합니다."""
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

KANANA_TIMEOUT = int(os.environ.get("KANANA_TIMEOUT", "55") or "55")

RETRIEVAL_TOP_K = 5
RETRIEVAL_MIN_SCORE = float(os.environ.get("RETRIEVAL_MIN_SCORE", "1.5"))

REDIS_HISTORY_KEY = "faq:chat:recent"
REDIS_HISTORY_LIMIT = 10

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
# 4. FAQ index (lazy — Vercel cold start 대비)
# ──────────────────────────────────────────────

_chunks: list[str] | None = None
_retriever: BM25 | None = None


def _env_clean(name: str) -> str:
    raw = (os.environ.get(name) or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def _kanana_api_key() -> str:
    return _env_clean("KANANA_API_KEY")


def _kanana_base_url() -> str:
    url = _env_clean("KANANA_BASE_URL") or (
        "https://kanana-o.a2s-endpoint.kr-central-2.kakaocloud.com/v1"
    )
    return url.rstrip("/")


def _kanana_model() -> str:
    return _env_clean("KANANA_MODEL") or "kanana-o"


def _ensure_index() -> None:
    global _chunks, _retriever
    if _retriever is not None:
        return
    print("[*] FAQ 문서 로딩 중...")
    faq_text = _load_faq_text()
    _chunks = _chunk_by_faq_number(faq_text)
    _retriever = BM25(_chunks)
    print(f"[+] 인덱싱 완료: {len(_chunks)}개 청크")

# ──────────────────────────────────────────────
# 5. Kanana-o LLM (direct HTTP, no openai lib)
# ──────────────────────────────────────────────

_ESCALATION_CONTACTS = """\
■ 대표문의
  고객센터 (02-6252-0000 / mplace@cj.net)

■ 영업문의 (메시징 일반, 세일즈포스, 컴원 연동)
  홍윤표님 (02-6252-0359 / yp.hong1@cj.net)
  한건영님 (02-6252-0487 / ky.han1@cj.net)

■ 정책문의
  김민정님 (02-6252-0733 / mj.kim129@cj.net)

■ 정산문의
  박지은님 (02-6252-0783 / jieun.park35@cj.net)
  류현애님 (02-6252-0816 / hyn4737@cj.net)

■ 계정발급
  류현애님 (02-6252-0816 / hyn4737@cj.net)

■ 발신번호 (승인담당)
  김수정님 (02-6252-0750 / sujung.kim16@cj.net)

■ 대량발송
  김수정님 (02-6252-0750 / sujung.kim16@cj.net)

■ 스팸대응
  김수정님 (02-6252-0750 / sujung.kim16@cj.net)

■ 개발문의
  · 엠플레이스: 서명주님 (02-6361-2841 / myeongjoo.seo@cj.net), 황주현님 (02-6252-0797 / juhyun.hwang@cj.net)
  · 컴원: 황주현님 (02-6252-0797 / juhyun.hwang@cj.net), 서명주님 (02-6361-2841 / myeongjoo.seo@cj.net)
  · APIPLEX: 황주현님 (02-6252-0797 / juhyun.hwang@cj.net)
  · 게이트웨이 / REST API 연동: 김동준님 (02-6361-2811 / dongjun.kim16@cj.net), 서명주님 (02-6361-2841 / myeongjoo.seo@cj.net)
  · Agent 연동: 강전호님 (02-6361-2844 / jeonho.kang@cj.net), 서명주님 (02-6361-2841 / myeongjoo.seo@cj.net)
  · 세일즈포스 연동: 서명주님 (02-6361-2841 / myeongjoo.seo@cj.net), 강전호님 (02-6361-2844 / jeonho.kang@cj.net)
  · 기술일반: 이민규님 (02-6252-0735 / mingyu.lee@cj.net)

■ 담당자 판단이 어려울 때
  mplace@cj.net 을 수신/참조로 보내시면 전체 담당자에게 전달되어 문의 처리를 지원합니다.
  개발 관련 문의는 devops@cj.net 을 수신/참조로 보내 주세요."""


def _no_faq_answer() -> str:
    return (
        "FAQ 문서에서 질문과 관련된 내용을 찾을 수 없습니다. "
        "추측하거나 임의로 답변드리지 않습니다. 아래 담당자에게 문의해 주세요.\n\n"
        + _ESCALATION_CONTACTS
    )


def _retrieval_is_weak(results: list[tuple[str, float]]) -> bool:
    if not results:
        return True
    return results[0][1] < RETRIEVAL_MIN_SCORE


_PROMPT_TEMPLATE = """\
당신은 사내 고객대응 FAQ 챗봇입니다. 아래 [FAQ 발췌] 내용만 근거로 답변하세요.

[필수 규칙]
1. FAQ에 없는 사실·숫자·절차·정책을 절대 지어내지 마세요. 확실하지 않으면 답하지 마세요.
2. 질문에 답할 근거가 FAQ 발췌에 없거나 부족하면, 반드시 아래 형식으로만 답하세요.
   - 첫 줄: "FAQ 문서에서 질문과 관련된 내용을 찾을 수 없습니다. 추측하여 답변드리지 않습니다."
   - 이어서 질문 주제에 맞는 담당자(아래 [담당자 연락처])만 골라 안내하세요. 해당 없으면 대표문의·판단 어려울 때 안내를 포함하세요.
   - 담당자 연락처는 아래 목록을 그대로 사용하고, 임의로 바꾸거나 생략하지 마세요.
3. FAQ에 근거가 충분할 때만 답변하세요. 접속 경로, 메뉴, URL 등 문서에 있는 구체적 절차를 포함하세요.

[FAQ 발췌]
--- 문서 1 ---
{doc1}

--- 문서 2 ---
{doc2}

--- 문서 3 ---
{doc3}

[담당자 연락처]
{contacts}

질문: {question}
답변:"""


def _call_kanana(question: str, context: list[str], history: list[dict]) -> tuple[str, str | None]:
    """반환: (답변 텍스트, 오류 종류 — auth|config|quota|server|api|network|None)"""
    api_key = _kanana_api_key()
    if not api_key:
        return (
            "⚠️ KANANA_API_KEY가 설정되지 않았습니다.\n"
            "Vercel → Settings → Environment Variables에 키를 등록한 뒤 Redeploy 해 주세요.",
            "config",
        )

    docs = context[:3] + ["(FAQ 발췌 없음)"] * (3 - len(context))
    prompt = _PROMPT_TEMPLATE.format(
        doc1=docs[0],
        doc2=docs[1],
        doc3=docs[2],
        contacts=_ESCALATION_CONTACTS,
        question=question,
    )
    messages = []
    for m in history[-4:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": prompt})

    body = json.dumps(
        {"model": _kanana_model(), "messages": messages}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{_kanana_base_url()}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=KANANA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {}
        err_body = detail.get("error")
        if isinstance(err_body, dict):
            err_msg = err_body.get("message") or str(err_body)
        else:
            err_msg = err_body or e.reason
        if code == 429:
            return (
                "⚠️ 일일 API 쿼터(10회)를 모두 소진하였습니다. 내일 00시에 초기화됩니다.",
                "quota",
            )
        if code in (401, 403):
            return (
                "⚠️ API 키가 유효하지 않습니다.\n"
                "1) Vercel Environment Variables의 KANANA_API_KEY에 신규 키만 입력(따옴표 없음)\n"
                "2) Production·Preview 모두 적용 여부 확인\n"
                "3) 저장 후 Redeploy\n"
                f"(서버 응답: {err_msg})",
                "auth",
            )
        if code == 500:
            return (
                "⚠️ GPU 서버 요청 폭주로 처리가 지연되고 있습니다. 잠시 후 다시 시도해 주세요.",
                "server",
            )
        return f"⚠️ API 오류 ({code}): {err_msg}", "api"
    except Exception as e:
        return f"⚠️ API 호출 중 오류 발생: {e}", "network"


# ──────────────────────────────────────────────
# 6. Upstash Redis (질문/답변 이력)
# ──────────────────────────────────────────────

def _redis_configured() -> bool:
    return bool(
        (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
        and (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    )


def _upstash_request(commands) -> dict | list | None:
    url = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
    token = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if not url or not token:
        return None
    body = json.dumps(commands, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[!] Upstash Redis 오류: {e}")
        return None


def _save_chat_history(question: str, answer: str) -> None:
    if not _redis_configured():
        return
    item = json.dumps(
        {
            "question": question,
            "answer": answer,
            "ts": int(time.time() * 1000),
        },
        ensure_ascii=False,
    )
    resp = _upstash_request(
        [
            ["LPUSH", REDIS_HISTORY_KEY, item],
            ["LTRIM", REDIS_HISTORY_KEY, "0", str(REDIS_HISTORY_LIMIT - 1)],
        ]
    )
    if resp is None:
        return
    if isinstance(resp, list):
        for part in resp:
            if isinstance(part, dict) and part.get("error"):
                print(f"[!] Upstash 저장 오류: {part['error']}")
    elif isinstance(resp, dict) and resp.get("error"):
        print(f"[!] Upstash 저장 오류: {resp['error']}")


def _get_chat_history() -> list[dict]:
    if not _redis_configured():
        return []
    resp = _upstash_request(["LRANGE", REDIS_HISTORY_KEY, "0", str(REDIS_HISTORY_LIMIT - 1)])
    if not resp:
        return []
    if isinstance(resp, list):
        raw = resp[0].get("result", []) if resp and isinstance(resp[0], dict) else []
    elif isinstance(resp, dict):
        if resp.get("error"):
            print(f"[!] Upstash 조회 오류: {resp['error']}")
            return []
        raw = resp.get("result") or []
    else:
        return []
    items: list[dict] = []
    for entry in raw:
        if isinstance(entry, bytes):
            entry = entry.decode("utf-8")
        try:
            items.append(json.loads(entry))
        except (json.JSONDecodeError, TypeError):
            continue
    return items


# ──────────────────────────────────────────────
# 7. Flask App
# ──────────────────────────────────────────────

app = Flask(__name__)


_HTML_TEMPLATE = open(
    os.path.join(BASE_DIR, "templates", "index.html"), encoding="utf-8"
).read()


@app.route("/")
def index():
    _ensure_index()
    html = _HTML_TEMPLATE.replace("{{ chunk_count }}", str(len(_chunks or [])))
    return Response(html, content_type="text/html; charset=utf-8")


@app.route("/api/chat", methods=["POST"])
def chat():
    _ensure_index()
    data = request.get_json()
    question = (data.get("question") or "").strip()
    history = data.get("history", [])

    if not question:
        return jsonify({"error": "질문을 입력해 주세요."}), 400

    results = _retriever.search(question, k=RETRIEVAL_TOP_K)  # type: ignore[union-attr]
    context_docs = [doc for doc, _ in results]
    refs = [
        {"text": doc[:300] + ("..." if len(doc) > 300 else ""), "score": round(sc, 2)}
        for doc, sc in results
    ]

    if _retrieval_is_weak(results):
        answer = _no_faq_answer()
        _save_chat_history(question, answer)
        return jsonify({"answer": answer, "references": refs})

    answer, err_kind = _call_kanana(question, context_docs, history)

    if err_kind in ("quota", "server", "api", "network") and context_docs:
        fallback = "🔍 API를 사용할 수 없어 FAQ 검색 결과를 직접 보여드립니다.\n\n"
        for i, doc in enumerate(context_docs[:3], 1):
            fallback += f"━━ 검색 결과 {i} ━━\n{doc}\n\n"
        answer = answer + "\n\n" + fallback

    _save_chat_history(question, answer)
    return jsonify({"answer": answer, "references": refs})


@app.route("/api/history", methods=["GET"])
def chat_history():
    return jsonify({"items": _get_chat_history(), "enabled": _redis_configured()})


@app.route("/api/status", methods=["GET"])
def api_status():
    """키 값은 노출하지 않고, 배포 환경 설정 여부만 확인합니다."""
    key = _kanana_api_key()
    return jsonify(
        {
            "kanana_key_set": bool(key),
            "kanana_key_length": len(key),
            "kanana_base_url": _kanana_base_url(),
            "kanana_model": _kanana_model(),
            "redis_enabled": _redis_configured(),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"[*] http://localhost:{port} 에서 챗봇을 시작합니다.")
    app.run(host="127.0.0.1", port=port, debug=False)
