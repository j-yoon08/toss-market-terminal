# Toss Market Terminal

토스증권 **공식 Open API**로 현재가·호가·체결·1분봉/일봉을 보여주는 조회 전용 실시간 터미널입니다. 넓은 터미널에서는 호가·차트·체결 3패널 관제 화면을, 좁은 SSH 터미널에서는 호가·체결 중심의 compact 화면을 제공합니다.

## 안전 경계

- 호출 가능 경로는 공개 시장 데이터 5개로 코드에 고정되어 있습니다.
- 계좌, 보유자산, 주문 생성·정정·취소 API를 호출하지 않습니다.
- 자격증명과 액세스 토큰을 로그 또는 화면에 출력하지 않습니다.
- 자격증명 파일이 일반 파일, 현재 사용자 소유, 정확히 `0600` 권한일 때만 실행됩니다.
- WebSocket 체결·호가에는 sequence가 없어 완전한 틱 복원용이 아닌 모니터링용입니다.

## 설치

```bash
cd /home/ubuntu/apps/toss-market-terminal
uv sync --dev
```

기본 자격증명 경로는 다음과 같습니다.

```text
/home/ubuntu/.config/tossinvest/openapi.json
```

파일 내용은 `client_id`, `client_secret` 키를 가져야 하며 권한은 `0600`이어야 합니다.

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

- `1`: 1분봉 차트
- `d`: 일봉 차트
- `r`: REST 스냅샷 재동기화
- `q`: 종료

화면에는 전일 종가 대비 변화, 당일 고가·저가·거래량, 최우선 호가 스프레드, 최근 체결 VWAP, 호가 잔량 막대, 실시간 체결 방향 및 마지막 tick 경과 시간이 표시됩니다.

실제 시세 기반 SVG 미리보기 생성:

```bash
uv run python scripts/capture_preview.py AAPL \
  --output-dir /home/ubuntu/Documents/outputs/toss-market-terminal
```

## 데이터 동작

1. OAuth2 Client Credentials로 메모리 내 액세스 토큰을 발급합니다.
2. REST로 종목·현재가·호가·최근 체결·1분봉·최근 40개 일봉 초기 상태를 조회합니다.
3. WebSocket에서 `trade:{kr|us}` 및 `orderbook:{kr|us}`를 구독합니다.
4. 구독 ack를 확인한 뒤 실시간 변경을 화면에 반영합니다.
5. 연결이 끊기면 제한된 지수 백오프로 재연결하고, TUI는 REST 상태를 다시 동기화합니다.

토큰 재발급은 기존 액세스 토큰을 무효화하므로 프로세스 내에서 만료 직전까지 재사용합니다.

## 개발 검증

```bash
uv run ruff check .
uv run pytest
```
