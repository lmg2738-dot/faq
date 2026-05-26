# FAQ 챗봇

고객대응 FAQ 문서 기반 RAG 챗봇 (Flask + BM25 + Kanana-o).

## Vercel 배포

1. [Vercel](https://vercel.com)에서 이 저장소를 Import 합니다.
2. **Settings → Environment Variables**에 아래 값을 등록합니다 (Production / Preview 모두 권장).

| 변수 | 필수 | 설명 |
|------|------|------|
| `KANANA_API_KEY` | 예 | Kanana API 토큰 (**Vercel에만 등록, Git/로컬 커밋 금지**) |
| `KANANA_BASE_URL` | 아니오 | 기본값: `https://kanana-o.a2s-endpoint.kr-central-2.kakaocloud.com/v1` |
| `KANANA_MODEL` | 아니오 | 기본값: `kanana-o` |
| `KANANA_TIMEOUT` | 아니오 | API 타임아웃(초). 기본 `55` (Vercel 함수 제한에 맞춤) |
| `UPSTASH_REDIS_REST_URL` | 아니오 | Upstash Redis REST URL (질문·답변 최근 10건 저장) |
| `UPSTASH_REDIS_REST_TOKEN` | 아니오 | Upstash Redis REST Token |

3. Deploy 후 배포 URL에서 챗봇을 사용합니다.

> **참고:** Kanana 응답이 길면 Vercel **Pro** 플랜에서 `maxDuration` 60초 설정이 필요할 수 있습니다. Hobby 플랜은 함수 실행 시간이 10초로 제한됩니다.

## 로컬 실행

```powershell
copy .env.example .env
# .env 에 KANANA_API_KEY 입력
python -m pip install -r requirements.txt
python app.py
```

브라우저: http://localhost:8080

## Windows 자동 실행 (로컬 PC)

```powershell
.\build.ps1          # 의존성 + 시작 프로그램 등록
.\uninstall_startup.ps1  # 자동 실행 해제
```
