#!/usr/bin/env python3
"""
빗썸 KRW 전 종목 WR(14) 알림 봇
- 주봉 WR(14)과 일봉 WR(14)이 '장중에' 동시에 -80 이하가 되면 텔레그램으로 알림
- 진행 중인 봉(이번 주 / 오늘)을 포함해 계산 → 빗썸 앱 차트 값과 같은 방식
- 외부 패키지 없이 파이썬 표준 라이브러리만 사용 (Python 3.9+)

사용법
  python3 wr_alert.py                 # 5분마다 계속 감시
  python3 wr_alert.py --once          # 한 바퀴만 돌고 종료 (cron용)
  python3 wr_alert.py --dry-run       # 텔레그램 대신 화면에 출력
  python3 wr_alert.py --check KRW-ELSA  # 특정 코인의 주봉/일봉 WR 값 확인 (앱 차트와 대조용)
  python3 wr_alert.py --test-telegram # 텔레그램 연결 테스트
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ───────────── 설정 (환경변수로 바꿀 수 있음) ─────────────
API = "https://api.bithumb.com/v1"
PERIOD = 14
THRESHOLD = float(os.getenv("WR_THRESHOLD", "-80"))
INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "300"))   # 감시 주기 (초)
REQ_PER_SEC = float(os.getenv("REQ_PER_SEC", "8"))           # 빗썸 한도(초당 150회)보다 한참 낮게
STATE_FILE = os.getenv("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wr_state.json"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ───────────── 빗썸 API ─────────────
_last_req = 0.0


def api_get(path, params):
    """요청 간격을 지키면서 GET. 429/5xx/네트워크 오류는 지수 백오프로 재시도."""
    global _last_req
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        wait = 1.0 / REQ_PER_SEC - (time.monotonic() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2 ** attempt)
    raise RuntimeError(f"요청 실패(재시도 초과): {url}")


def krw_markets():
    data = api_get("/market/all", {"isDetails": "false"})
    return {d["market"]: d.get("korean_name", "") for d in data if d["market"].startswith("KRW-")}


def candles(market, unit):
    """unit: 'weeks' 또는 'days'. 최신 봉이 [0] (진행 중인 봉 포함)."""
    return api_get(f"/candles/{unit}", {"market": market, "count": PERIOD})


# ───────────── 지표 계산 ─────────────
def williams_r(cs, period=PERIOD):
    """
    WR = (최근 N봉 최고가 − 현재가) / (최근 N봉 최고가 − 최근 N봉 최저가) × (−100)
    cs: 최신순 캔들 리스트. 봉이 부족하거나 변동이 0이면 None.
    """
    if len(cs) < period:
        return None
    window = cs[:period]
    hh = max(float(c["high_price"]) for c in window)
    ll = min(float(c["low_price"]) for c in window)
    close = float(window[0]["trade_price"])
    if hh == ll:
        return None
    return (hh - close) / (hh - ll) * -100.0


def evaluate(market, full=False):
    """
    주봉을 먼저 보고(더 드물게 -80 아래로 감), 통과한 코인만 일봉을 조회 → 요청 수 절약.
    full=True면 조건과 상관없이 둘 다 계산.
    """
    w = candles(market, "weeks")
    wr_w = williams_r(w)
    price = float(w[0]["trade_price"]) if w else None
    if not full and (wr_w is None or wr_w > THRESHOLD):
        return {"wr_w": wr_w, "wr_d": None, "price": price, "hit": False}
    wr_d = williams_r(candles(market, "days"))
    hit = wr_w is not None and wr_d is not None and wr_w <= THRESHOLD and wr_d <= THRESHOLD
    return {"wr_w": wr_w, "wr_d": wr_d, "price": price, "hit": hit}


# ───────────── 상태 저장 (중복 알림 방지) ─────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("active", {})    # 직전 확인 때 조건 충족 여부
    s.setdefault("alerted", {})   # 마지막 알림 날짜(KST)
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ───────────── 텔레그램 ─────────────
def send_telegram(text, dry=False):
    if dry or not (TG_TOKEN and TG_CHAT):
        print("─── 알림 ───\n" + text + "\n────────────", flush=True)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # 텔레그램 메시지 길이 제한(4096자) 대비 분할
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": chunk}).encode()
        for attempt in range(3):
            try:
                urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10).read()
                break
            except Exception as e:
                log(f"텔레그램 전송 실패({attempt + 1}/3): {e}")
                time.sleep(2 ** attempt)


def fmt_price(p):
    return f"{p:,.10g}" if p is not None else "-"


# ───────────── 한 바퀴 감시 ─────────────
def run_cycle(state, dry=False):
    t0 = time.monotonic()
    markets = krw_markets()
    today = datetime.now(KST).date().isoformat()
    hits, errors = [], 0

    for m, name in markets.items():
        try:
            r = evaluate(m)
        except Exception as e:
            errors += 1
            log(f"{m} 조회 실패: {e}")
            continue  # 실패한 코인은 상태를 건드리지 않음
        was = state["active"].get(m, False)
        # 조건이 '새로' 충족됐을 때만 + 같은 코인은 하루 1번만
        if r["hit"] and not was and state["alerted"].get(m) != today:
            hits.append((m, name, r))
            state["alerted"][m] = today
        state["active"][m] = r["hit"]

    # 상장폐지된 마켓 정리
    for k in ("active", "alerted"):
        state[k] = {m: v for m, v in state[k].items() if m in markets}
    save_state(state)

    if hits:
        lines = [f"🔔 주봉·일봉 WR({PERIOD}) ≤ {THRESHOLD:g} 동시 터치 ({len(hits)}개)", ""]
        for m, name, r in sorted(hits, key=lambda x: x[2]["wr_w"]):
            sym = m.split("-", 1)[1]
            lines.append(f"• {name} ({sym})  {fmt_price(r['price'])}원")
            lines.append(f"   주봉 {r['wr_w']:.1f} / 일봉 {r['wr_d']:.1f}")
        send_telegram("\n".join(lines), dry)

    log(f"{len(markets)}개 확인, 신규 알림 {len(hits)}개, 실패 {errors}개, "
        f"{time.monotonic() - t0:.0f}초 소요")


def main():
    global THRESHOLD
    ap = argparse.ArgumentParser(description="빗썸 주봉·일봉 WR(14) 동시 과매도 알림")
    ap.add_argument("--once", action="store_true", help="한 바퀴만 돌고 종료")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 대신 화면 출력")
    ap.add_argument("--check", metavar="MARKET", help="특정 코인 WR 확인 (예: KRW-ELSA)")
    ap.add_argument("--test-telegram", action="store_true", help="텔레그램 테스트 메시지 전송")
    ap.add_argument("--threshold", type=float, help=f"기준값 (기본 {THRESHOLD:g})")
    a = ap.parse_args()
    if a.threshold is not None:
        THRESHOLD = a.threshold

    if a.test_telegram:
        if not (TG_TOKEN and TG_CHAT):
            sys.exit("TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경변수를 먼저 설정하세요.")
        send_telegram("✅ WR 알림 봇 연결 테스트")
        return

    if a.check:
        r = evaluate(a.check.upper(), full=True)
        f = lambda v: "계산 불가(봉 부족)" if v is None else f"{v:.2f}"
        print(f"{a.check.upper()}  현재가 {fmt_price(r['price'])}원\n"
              f"  주봉 WR({PERIOD}): {f(r['wr_w'])}\n  일봉 WR({PERIOD}): {f(r['wr_d'])}\n"
              f"  알림 조건(둘 다 ≤ {THRESHOLD:g}): {'충족' if r['hit'] else '미충족'}")
        return

    if not a.dry_run and not (TG_TOKEN and TG_CHAT):
        log("텔레그램 설정이 없어 화면 출력 모드로 실행합니다.")

    state = load_state()
    log(f"시작: 기준 {THRESHOLD:g}, 주기 {INTERVAL_SEC}초")
    while True:
        try:
            run_cycle(state, a.dry_run)
        except Exception as e:
            log(f"사이클 오류: {e}")
        if a.once:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
