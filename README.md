# FAQ 챗봇

고객대응 FAQ 문서 기반 RAG 챗봇 (Flask + BM25 + OpenRouter 무료 모델).

## Vercel 배포

1. [Vercel](https://vercel.com)에서 이 저장소를 Import 합니다.
2. **Settings → Environment Variables**에 아래 값을 등록합니다 (Production / Preview 모두 권장).

| 변수 | 필수 | 설명 |
|------|------|------|
| `OPENROUTER_API_KEY` | 예 | OpenRouter API 키 (**Vercel에만 등록, Git/로컬 커밋 금지**) |
| `OPENROUTER_TIMEOUT` | 아니오 | API 타임아웃(초). 기본 `55` |
| `OPENROUTER_SITE_URL` | 아니오 | OpenRouter Referer (배포 URL 권장) |
| `OPENROUTER_APP_TITLE` | 아니오 | OpenRouter 앱 이름. 기본 `FAQ Chatbot` |
| `UPSTASH_REDIS_REST_URL` | 아니오 | Upstash Redis REST URL (질문·답변 최근 10건 저장) |
| `UPSTASH_REDIS_REST_TOKEN` | 아니오 | Upstash Redis REST Token |

3. Deploy 후 배포 URL에서 챗봇을 사용합니다.

> UI에서 **OpenRouter 무료($0) 모델**만 선택할 수 있습니다. 모델 한도/만료 시 다음 무료 모델로 자동 전환되며, 사용 불가 모델은 목록에서 선택할 수 없습니다.

## 로컬 실행

```powershell
copy .env.example .env
# .env 에 OPENROUTER_API_KEY 입력
python -m pip install -r requirements.txt
python app.py
```

브라우저: http://localhost:8080

## Windows 자동 실행 (로컬 PC)

```powershell
.\build.ps1
.\uninstall_startup.ps1
```
