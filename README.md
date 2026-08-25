# Toss Market Terminal

토스증권 **공식 Open API**로 현재가·호가·체결·1분봉을 보여주는 조회 전용 터미널 프로그램입니다.

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

- `r`: REST 스냅샷 재동기화
- `q`: 종료

## 데이터 동작

1. OAuth2 Client Credentials로 메모리 내 액세스 토큰을 발급합니다.
2. REST로 종목·현재가·호가·최근 체결·1분봉 초기 상태를 조회합니다.
3. WebSocket에서 `trade:{kr|us}` 및 `orderbook:{kr|us}`를 구독합니다.
4. 구독 ack를 확인한 뒤 실시간 변경을 화면에 반영합니다.
5. 연결이 끊기면 제한된 지수 백오프로 재연결하고, TUI는 REST 상태를 다시 동기화합니다.

토큰 재발급은 기존 액세스 토큰을 무효화하므로 프로세스 내에서 만료 직전까지 재사용합니다.

## 개발 검증

```bash
uv run ruff check .
uv run pytest
```
