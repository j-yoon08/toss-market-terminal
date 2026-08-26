# Toss Market Terminal

토스증권 **공식 Open API**로 관심 종목·현재가·호가·체결·캔들 차트를 보여주는 조회 전용 실시간 터미널입니다. 넓은 터미널에서는 관심 종목·호가·차트·체결을 함께 표시하고, 좁은 SSH 터미널에서는 호가·체결 중심의 compact 화면으로 전환합니다.

## v0.7b 주요 기능 (PAPER 주문 미리보기 티켓)

- TUI에서 `b`(매수) / `s`(매도) 키로 PAPER 주문 미리보기 티켓 모달을 엽니다. 현재가·통화가 없으면 fail-closed로 열리지 않습니다.
- LIMIT(지정가)은 수량 `Enter` → 지정가 입력으로 이동 → 지정가 `Enter`로 확인 화면, MARKET(시장가)은 수량 `Enter`로 바로 확인 화면입니다.
- 티켓 안에서는 포커스 중인 입력란에서도 `m`이 글자로 입력되지 않고 LIMIT↔MARKET 전환으로 동작합니다(MARKET에서는 지정가 입력 비활성·값 비움).
- 미리보기 생성 결과는 확인 모달(`OrderConfirmScreen`)에서 `Enter`로 로컬 확정할 수 있으며, 이는 메모리 보관 + 알림일 뿐 **어떤 주문도 전송되지 않습니다**(`PAPER_PREVIEW · 실제 주문 전송 없음` 배너 상시 표시).
- 티켓·확인 모달은 `a/c/j/k/q/r/s/b` 등 앱 바인딩 키를 no-op 바인딩으로 막아 아래 화면으로 새지 않습니다.
- 응답 지연 사이 종목이 바뀌면 stale 검사로 미리보기를 만들지 않고, 중복 `b/s` 입력은 잠금으로 직렬화합니다.
- 검증 실패·조회 실패 메시지는 원문 계좌번호·토큰을 노출하지 않는 짧은 한국어 한 줄로 정제됩니다.

## v0.7a 주요 기능 (paper preview 경계)

- `order_preview` 모듈: 주문 **미리보기 전용** 도메인 계층으로, HTTP 클라이언트 의존성이 없고 어떤 주문 엔드포인트도 알지 못하며 호출할 수 없습니다(전송·재시도 불가).
- `PaperPreviewService`는 검증된 미리보기만 생성하며 `mode=PAPER_PREVIEW`, `order_endpoint_called=false`, `automatic_retry=false`, `manual_approval_only=true`를 항상 명시합니다.
- 엄격한 Decimal 입력 파서(문자열/정수만, bool·float·NaN·Infinity·0·음수·과길이 텍스트 거부)와 시장별 수량 규칙(KR 정수, US LIMIT/MARKET 매수 정수, US MARKET 매도만 소수) 적용
- 리스크 게이트 fail-closed: BUY는 추정 금액 ≤ 매수가능금액, SELL은 수량 ≤ 보유 수량, KRW/USD 외 조합 거부, 단일 주문 안전 상한(기본 KRW 100,000 / USD 100 — 설정 가능한 안전 장치이며 투자 권고가 아님)
- 원문 계좌번호·토큰은 저장 구조상 불가능하고 마스킹된 계좌번호만 보관되며, 직렬화·오류 메시지에도 원문이 노출되지 않습니다
- 주문 의도 불변 필드 + 안전 정책 버전의 canonical JSON SHA-256 지문(타임스탬프·비밀 제외)과 사람이 확인하는 승인 문구(`APPROVE <SIDE> <SYMBOL> <수량> <지문 앞 8자>`) 제공

## v0.6 주요 기능

- `toss-market account SYMBOL` 읽기 전용 계좌 조회: 보유 종목 정보와 매수가능금액을 한 번에 확인
- `--account-seq N`으로 계좌를 지정하지 않으면 종합매매(BROKERAGE) 계좌가 정확히 하나일 때 자동 선택
- `--json` 출력에는 `scope=account_read_only`와 `order_endpoints_called=false`가 항상 포함되어 조회 경계를 명시
- 계좌번호는 어디에도(화면·JSON·오류·로그) 원문으로 나오지 않고 뒤 4자리만 남긴 마스킹(`*******8901`)으로만 표시
- 매수가능금액은 KRW/USD만 지원하며, 응답 통화가 요청 통화와 다르면 안전하게 실패(fail-closed)
- 보유 자산 통화가 KRW/USD가 아니면 매수가능금액 요청 전에 조회를 중단

## v0.5 주요 기능

- 실제 캔들스틱 + 거래량 막대 차트와 우측 가격축·하단 시간축, 현재가·전일 종가 기준선
- WebSocket 체결을 최신 1분봉 OHLCV와 일봉에 반영하고 차트를 최대 초당 4회 갱신, 30초마다 REST 캔들로 누락·중복 보정
- `1`/`2`/`3`/`4`/`5` 키로 1분/5분/15분/1시간/일봉 타임프레임 전환, `c` 키로 차트 포커스 레이아웃
- 차트 중심의 15/24/42/19 패널 비율과 압축된 종목·시장 신호 요약
- 선택한 타임프레임 기준 EMA9/21, RSI14, 거래량 배수, 세션 VWAP, 지지·저항 지표를 한글로 표시
- 최대 12개 관심 종목을 로컬 설정에 저장, 15초마다 공식 배치 API로 현재가 갱신
- `>` 마커로 활성 종목을 표시하고 방향키 또는 `j`/`k`와 Enter로 종목 전환
- 목표가·등락률·거래량 급증 로컬 알림
- 호가 불균형, 매수/매도 잔량 비율, VWAP 거리, 거래량 배수, 체결 압력 표시
- 상태바에 연결 상태와 마지막 TICK·SYNC 경과 시간을 분리 표시
- 간결한 Footer와 `?` 도움말 모달

## 안전 경계

- 호출 가능한 REST 경로는 공개 시장 데이터 5개와 계좌 조회 3개(`/api/v1/accounts`, `/api/v1/holdings`, `/api/v1/buying-power`)로 코드에 고정되어 있으며, 모두 GET 전용입니다.
- 주문 생성·정정·취소 등 POST를 포함한 모든 변경 요청은 클라이언트 구조상 발생할 수 없습니다.
- 계좌 조회는 읽기 전용 스코프(`account_read_only`)로만 동작하며 주문 API는 호출하지 않습니다.
- 자격증명과 액세스 토큰을 로그·설정·화면에 출력하지 않습니다.
- 자격증명 파일이 일반 파일, 현재 사용자 소유, 정확히 `0600` 권한일 때만 실행됩니다.
- 관심 종목과 알림만 저장하는 설정 파일은 원자적으로 저장되며 파일 `0600`, 디렉터리 `0700` 권한을 사용합니다.
- 알림은 `watch`가 실행 중일 때만 발생합니다. 백그라운드 서비스나 주문 자동화가 아닙니다.
- WebSocket 체결·호가에는 sequence가 없어 완전한 틱 복원용이 아닌 모니터링용입니다.
- 화면의 `매수 우세`, `매도 우세`, `상승 우세`, EMA/RSI/지지·저항 등의 지표는 표시 데이터에서 계산한 객관적 관찰값이며 매수·매도 추천이나 예측이 아닙니다.

## 설치

```bash
cd /home/ubuntu/apps/toss-market-terminal
uv sync --dev
```

기본 자격증명 경로:

```text
/home/ubuntu/.config/tossinvest/openapi.json
```

파일 내용은 `client_id`, `client_secret` 키를 가져야 하며 권한은 `0600`이어야 합니다.

관심 종목과 알림의 기본 설정 경로:

```text
~/.config/toss-market/settings.json
```

다른 설정 파일을 사용하려면 서브커맨드 앞에 `--settings PATH`를 지정합니다.

## 관심 종목

```bash
uv run toss-market watchlist list
uv run toss-market watchlist add AAPL
uv run toss-market watchlist add NVDA
uv run toss-market watchlist add 005930
uv run toss-market watchlist remove NVDA
```

테스트용 별도 설정 파일:

```bash
uv run toss-market --settings /tmp/toss-market-settings.json watchlist add AAPL
```

`toss-market watch SYMBOL`의 실행 심볼은 화면의 관심 목록에 일시적으로 포함되지만 설정 파일에 자동 저장되지는 않습니다.

실행 중인 TUI에서는 `a`를 누르고 심볼을 입력한 뒤 `Enter`를 누르면 관심 종목에 저장되고 목록에 즉시 반영됩니다. `Esc`는 입력을 취소합니다. 공식 API에 연결된 상태에서는 저장 전에 종목 존재 여부를 확인합니다.

## 로컬 알림

```bash
uv run toss-market alert list
uv run toss-market alert add AAPL above 250
uv run toss-market alert add AAPL below 200
uv run toss-market alert add AAPL change-above 5
uv run toss-market alert add AAPL change-below 5
uv run toss-market alert add AAPL volume-spike 3
uv run toss-market alert remove A1
```

알림 의미:

- `above 250`: 가격이 250 이하에서 250 초과로 전환
- `below 200`: 가격이 200 이상에서 200 미만으로 전환
- `change-above 5`: 전일 종가 대비 등락률이 +5%를 초과
- `change-below 5`: 전일 종가 대비 등락률이 -5% 미만
- `volume-spike 3`: 최신 1분봉 거래량이 직전 유효 봉 중앙값의 3배를 초과

첫 관측은 상태만 설정해 시작 직후 알림 폭주를 막습니다. 조건이 계속 참이면 반복하지 않고, 조건이 거짓으로 돌아간 뒤 다시 돌파할 때 재알림합니다. 목표가 알림은 관심 종목 배치 가격에도 적용되며, 등락률·거래량 알림은 필요한 전체 스냅샷이 있는 활성 종목에서 평가합니다.

## 사용

REST 스냅샷:

```bash
uv run toss-market snapshot AAPL
uv run toss-market snapshot 005930
```

읽기 전용 계좌 조회 (v0.6):

```bash
uv run toss-market account AAPL --json
uv run toss-market account 005930 --account-seq 1
```

- 계좌를 지정하지 않으면 종합매매(BROKERAGE) 계좌가 정확히 하나일 때 자동으로 선택하고, 여러 개이면 `--account-seq` 지정을 안내합니다.
- JSON 출력에는 `scope=account_read_only`, `order_endpoints_called=false`가 항상 포함되고 계좌번호는 마스킹(`*******8901`)만 나옵니다.
- 보유 종목이 없으면 보유 수량 0으로 취급하고, 심볼 시장(KR 6자리 숫자 → KRW, 그 외 → USD)으로 매수가능금액을 조회합니다.
- 보유 자산 통화가 KRW/USD가 아니면 매수가능금액 조회 전에 오류로 종료합니다.

실시간 TUI:

```bash
uv run toss-market watch AAPL
uv run toss-market watch 005930
```

연결 검증:

```bash
uv run toss-market probe AAPL --seconds 8
```

TUI 키:

- `↑` / `k`: 관심 목록 위로 이동
- `↓` / `j`: 관심 목록 아래로 이동
- `Enter`: 선택한 종목으로 전환
- `a`: 관심 종목 추가 입력창
- `b`: PAPER 매수 미리보기 티켓(주문 전송 없음)
- `s`: PAPER 매도 미리보기 티켓(주문 전송 없음)
- `1`: 1분봉 차트
- `2`: 5분봉 차트
- `3`: 15분봉 차트
- `4`: 1시간봉 차트
- `5`: 일봉 차트
- `c`: 차트 포커스 레이아웃 전환(넓은 화면에서 관심 목록을 숨기고 차트를 확대)
- `?`: 전체 키보드 도움말 열기/닫기
- `r`: REST 스냅샷 재동기화
- `q`: 종료

PAPER 주문 미리보기 티켓(v0.7b)은 **paper preview 전용**입니다. 티켓 안 키:

- 수량 입력 후 `Enter`: LIMIT은 지정가 입력으로 이동, MARKET은 바로 확인 화면
- 지정가 입력 후 `Enter`: 확인 화면(LIMIT만)
- `m`: LIMIT ↔ MARKET 전환(입력란 포커스 중에도 유형 전환으로 동작)
- `Esc`: 티켓/확인 취소 — 아무 것도 저장하지 않음
- 확인 화면에서 `Enter`: PAPER 미리보기를 메모리에 보관하고 알림을 띄울 뿐, **실제 주문은 전송되지 않습니다**(`PAPER_PREVIEW` 배너·도움말로 상시 표시)

`LIVE TRADES`의 시간은 밀리초를 생략한 `HH:MM:SS` 형식으로 표시합니다. 같은 초의 체결은 화면 행 순서대로 최신순입니다.

상태바의 `TICK`은 마지막 WebSocket 체결 이후 경과 시간, `SYNC`는 마지막으로 성공한 REST 스냅샷 또는 캔들 재동기화 이후 경과 시간입니다. 아직 관측되지 않았거나 동기화가 실패한 값은 만들지 않고 `—` 또는 직전 성공 시각 기준으로 표시합니다.

## 화면 신호

시장 신호 요약 패널은 다음과 같이 한글로 표시합니다.

- `매수·매도 호가 차이`: 최우선 매도호가와 매수호가의 차이
- `체결 평균`: 최근 체결의 거래량 가중 평균과 현재가의 거리
- `호가 매수 우세` / `호가 매도 우세` / `호가 수급 균형`: 표시 중인 7단계 호가의 매수 잔량 비중
- `잔량비`: 매수 잔량 / 매도 잔량 비율
- `체결 상승 우세` / `체결 하락 우세` / `체결 방향 혼조`: 가격이 오른 체결과 내린 체결의 거래량 비중
- `1분 거래량`: 최신 1분봉 거래량 / 이전 유효 봉 중앙값

데이터가 없거나 분모가 0이면 수치를 만들지 않고 `—` 또는 `데이터 부족`으로 표시합니다.

## 차트 지표

차트 패널 아래에는 현재 선택된 타임프레임(1분/5분/15분/1시간/일봉) 기준으로 계산한 지표 요약이 함께 표시됩니다. 모두 표시 데이터에서 계산한 객관적 관찰값이며 매수·매도 추천이나 예측이 아닙니다.

- `EMA9/21 단기선 위` / `단기선 아래` / `단기선 겹침`: 9기간 지수이동평균이 21기간 지수이동평균보다 높은지·낮은지·같은지
- `RSI`: 14기간 상대강도지수 값과 구간(`30` 미만 `낮은 구간`, `70` 초과 `높은 구간`, 그 외 `중립 구간`)
- `거래량`: 최신 캔들 거래량 / 직전 유효 캔들 중앙값 배수 (`N.N배`)
- `VWAP`: 세션(당일) 거래량 가중 평균가와 현재가의 거리. 일봉 모드는 세션 개념이 없어 항상 `VWAP 데이터 부족`으로 표시하며 값을 임의로 만들지 않습니다.
- `지지` / `저항`: 전일 종가·세션 저가/고가·최근 저가/고가·스윙 저점/고점 중 현재가 이하 최고값(지지)과 현재가 이상 최저값(저항). 유효한 후보가 없으면 `—`

지표 계산에 필요한 캔들이 부족하면(예: 신규 상장 직후, 캔들 수 부족) 값을 `0`으로 만들지 않고 `—` 또는 `데이터 부족`으로 표시합니다.

## 데이터 동작

1. OAuth2 Client Credentials로 메모리 내 액세스 토큰을 발급합니다.
2. REST로 활성 종목·현재가·호가·최근 체결·최근 200개 1분봉·최근 200개 일봉을 조회합니다.
3. WebSocket 체결을 현재 1분봉의 고가·저가·종가·거래량에 합성하고 새 분이 시작되면 새 캔들을 추가합니다. 5분·15분·1시간봉은 이 1분봉에서 다시 집계됩니다.
4. 차트·기술 지표 렌더는 고빈도 체결에서도 최대 초당 4회로 합쳐 처리하며 마지막 체결은 trailing render로 반영합니다.
5. 30초마다 공식 1분봉·일봉 REST 데이터를 다시 받아 WebSocket의 sequence 부재로 생길 수 있는 누락·중복을 보정합니다. REST 요청 중 도착한 체결은 응답 위에 재적용합니다.
6. 관심 종목 현재가는 `/api/v1/prices?symbols=...` 배치 요청으로 갱신합니다.
7. WebSocket에서 활성 종목의 `trade:{kr|us}` 및 `orderbook:{kr|us}`를 구독합니다.
8. 종목 전환 시 기존 피드와 대기 중인 차트 렌더를 취소·대기한 뒤 새 스냅샷과 피드를 시작합니다.
9. 연결이 끊기면 제한된 지수 백오프로 재연결하고 REST 상태를 다시 동기화합니다.

`account` 서브커맨드는 위 실시간 파이프라인과 독립적이며, 계좌 조회 3개 GET 엔드포인트만 순서대로 호출하고 종료합니다. 테스트는 모두 `httpx.MockTransport`로 수행하며 실제 계좌 조회를 실행하지 않습니다.

토큰 재발급은 기존 액세스 토큰을 무효화하므로 프로세스 내에서 만료 직전까지 재사용합니다.

## 실제 시세 기반 미리보기

```bash
uv run python scripts/capture_preview.py AAPL \
  --output-dir /home/ubuntu/Documents/outputs/toss-market-terminal \
  --width 140 --height 42
```

## 개발 검증

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```
