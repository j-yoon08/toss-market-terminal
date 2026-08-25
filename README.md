# Toss Market Terminal

토스증권 **공식 Open API**로 관심 종목·현재가·호가·체결·캔들 차트를 보여주는 조회 전용 실시간 터미널입니다. 넓은 터미널에서는 관심 종목·호가·차트·체결을 함께 표시하고, 좁은 SSH 터미널에서는 호가·체결 중심의 compact 화면으로 전환합니다.

## v0.4 주요 기능

- 실제 캔들스틱 + 거래량 막대 차트 렌더링, 최근 200개 캔들 조회
- WebSocket 체결을 최신 1분봉 OHLCV와 일봉에 반영하고 차트를 최대 초당 4회 갱신, 30초마다 REST 캔들로 누락·중복 보정
- `1`/`5`/`i`/`h`/`d` 키로 1분/5분/15분/1시간/일봉 타임프레임 전환, `c` 키로 차트 포커스 레이아웃
- 선택한 타임프레임 기준 EMA9/21, RSI14, 거래량 배수, 세션 VWAP, 지지·저항 지표를 차트 옆에 한글 요약으로 표시
- 최대 12개 관심 종목을 로컬 설정에 저장, 15초마다 공식 배치 API로 현재가 갱신
- 방향키 또는 `j`/`k`와 Enter로 활성 종목 전환
- 목표가·등락률·거래량 급증 로컬 알림
- 호가 불균형, 매수/매도 잔량 비율, VWAP 거리, 거래량 배수, 체결 압력 표시
- REST/WebSocket 재동기화 유지

## 안전 경계

- 호출 가능한 REST 경로는 공개 시장 데이터 5개로 코드에 고정되어 있습니다.
- 계좌, 보유자산, 주문 생성·정정·취소 API를 호출하지 않습니다.
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
- `1`: 1분봉 차트
- `5`: 5분봉 차트
- `i`: 15분봉 차트
- `h`: 1시간봉 차트
- `d`: 일봉 차트
- `c`: 차트 포커스 레이아웃 전환(넓은 화면에서 관심 목록을 숨기고 차트를 확대)
- `r`: REST 스냅샷 재동기화
- `q`: 종료

`LIVE TRADES`의 시간은 밀리초를 생략한 `HH:MM:SS` 형식으로 표시합니다. 같은 초의 체결은 화면 행 순서대로 최신순입니다.

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
