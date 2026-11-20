#!/usr/bin/env python3
"""
빗썸 KRW 코인 '과매도 반등' 신호 알림 + 가상 매매 기록 봇

[매수 신호]
  주봉 WR(14)과 일봉 WR(14)이 둘 다 -85 이하인 날이 14일 이상 이어진 뒤,
  처음으로 일봉이 상승 마감(전일 종가보다 높게 마감)한 날
  + 최근 20일 평균 하루 거래대금 1억원 이상
  → 그날 종가를 기준가로 알림

[매도 기준 (가상 매매)]
  익절 +15% / 손절 -10% / 최대 30일 보유 (같은 날 둘 다 닿으면 손절로 처리)

백테스트(빗썸 2023-02 ~ 2026-11)에서 고른 조건이며, 미래 수익을 보장하지 않습니다.
외부 패키지 없이 파이썬 표준 라이브러리만 사용 (Python 3.9+)

사용법
  python3 wr_alert.py                 # 신호 확인 + 가상 매매 결과 업데이트 (하루 1번, 자정 직후 실행)
  python3 wr_alert.py --dry-run       # 텔레그램 대신 화면 출력
  python3 wr_alert.py --check KRW-BTC # 특정 코인의 현재 상태 확인
"""
import argparse
import csv
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# ───────────── 전략 설정 ─────────────
WR_TH = float(os.getenv("WR_THRESHOLD", "-85"))   # 주봉·일봉 WR 기준
RUN_DAYS = int(os.getenv("RUN_DAYS", "14"))        # 기준 이하 연속 일수
MIN_LIQ = float(os.getenv("MIN_LIQ", "1e8"))       # 20일 평균 하루 거래대금 하한 (원)
TP = float(os.getenv("TAKE_PROFIT", "0.15"))       # 익절 +15%
SL = float(os.getenv("STOP_LOSS", "0.10"))         # 손절 -10%
MAX_HOLD = int(os.getenv("MAX_HOLD_DAYS", "30"))   # 최대 보유 일수
FEE = 0.001                                        # 가상 결과 계산용 수수료 (편도)
PERIOD = 14
CANDLES = 200
EXCLUDE = {"USDT", "USDC", "USDE", "USDS", "USD1", "USDG", "DAI", "PAXG", "XAUT", "FDUSD", "PYUSD", "TUSD"}

# ───────────── 실행 설정 ─────────────
API = "https://api.bithumb.com/v1"
REQ_PER_SEC = float(os.getenv("REQ_PER_SEC", "20"))
WORKERS = int(os.getenv("WORKERS", "10"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.getenv("STATE_FILE", os.path.join(BASE_DIR, "wr_state.json"))
TRADES_FILE = os.getenv("TRADES_FILE", os.path.join(BASE_DIR, "trades.csv"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ───────────── 빗썸 API ─────────────
_next_slot = 0.0
_slot_lock = threading.Lock()


def _throttle():
    global _next_slot
    with _slot_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + 1.0 / REQ_PER_SEC
    if slot > now:
        time.sleep(slot - now)


def api_get(path, params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(3):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "wr-alert/2.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = repr(e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"요청 실패({last_err}): {url}")


def krw_markets():
    data = api_get("/market/all", {"isDetails": "false"})
    return {d["market"]: d.get("korean_name", "") for d in data
            if d["market"].startswith("KRW-") and d["market"][4:] not in EXCLUDE}


def daily_rows(market, today):
    """마감된 일봉만, 오래된 순. 각 행: (날짜, 고가, 저가, 종가, 거래대금)"""
    data = api_get("/candles/days", {"market": market, "count": CANDLES})
    rows = {}
    for c in data:
        t = c["candle_date_time_kst"][:10]
        if t >= today:          # 진행 중인 오늘 봉은 제외
            continue
        rows[t] = (t, float(c["high_price"]), float(c["low_price"]),
                   float(c["trade_price"]), float(c["candle_acc_trade_price"]))
    return [rows[k] for k in sorted(rows)]


# ───────────── 지표 · 신호 계산 (백테스트와 같은 방식) ─────────────
def week_key(t):
    d = datetime.strptime(t, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def indicators(rows):
    n = len(rows)
    t = [r[0] for r in rows]; h = [r[1] for r in rows]; l = [r[2] for r in rows]
    c = [r[3] for r in rows]; v = [r[4] for r in rows]
    wr_d = [None] * n
    wr_w = [None] * n
    liq = [0.0] * n
    wk, w_hi, w_lo = [], [], []
    s = 0.0
    for i in range(n):
        if i >= PERIOD - 1:
            hh = max(h[i - PERIOD + 1:i + 1]); ll = min(l[i - PERIOD + 1:i + 1])
            if hh > ll:
                wr_d[i] = (hh - c[i]) / (hh - ll) * -100
        key = week_key(t[i])
        if not wk or wk[-1] != key:
            wk.append(key); w_hi.append(h[i]); w_lo.append(l[i])
        else:
            w_hi[-1] = max(w_hi[-1], h[i]); w_lo[-1] = min(w_lo[-1], l[i])
        k = len(wk) - 1
        if k >= PERIOD - 1:
            hh = max([w_hi[k]] + w_hi[k - PERIOD + 1:k]); ll = min([w_lo[k]] + w_lo[k - PERIOD + 1:k])
            if hh > ll:
                wr_w[i] = (hh - c[i]) / (hh - ll) * -100
        liq[i] = s / min(20, i) if i else 0.0      # 직전 20일 평균 (당일 제외)
        s += v[i]
        if i >= 20:
            s -= v[i - 20]
    return t, h, l, c, wr_d, wr_w, liq


def evaluate(rows):
    """마지막 마감일 기준 상태와 신호 여부"""
    t, h, l, c, wr_d, wr_w, liq = indicators(rows)
    n = len(rows)
    both = lambda i: wr_d[i] is not None and wr_w[i] is not None and wr_d[i] <= WR_TH and wr_w[i] <= WR_TH
    run, fired, signal, run_at_signal = 0, False, False, 0
    for i in range(1, n):
        if run >= RUN_DAYS and not fired and c[i] > c[i - 1]:
            fired = True
            if i == n - 1:
                signal, run_at_signal = True, run
        if both(i):
            run += 1
        else:
            run, fired = 0, False
    L = n - 1
    return {"date": t[L], "close": c[L], "wr_d": wr_d[L], "wr_w": wr_w[L], "liq": liq[L],
            "run": run, "signal": signal and liq[L] >= MIN_LIQ, "raw_signal": signal, "run_at_signal": run_at_signal}


def resolve(trade, rows):
    """가상 매매 결과. 진입일 다음 날부터 순서대로: 손절 → 익절 → 기간 만료"""
    after = [r for r in rows if r[0] > trade["date"]]
    for k, (t, hi, lo, cl, _) in enumerate(after, start=1):
        if lo <= trade["sl"]:
            return {"result": "손절", "exit_date": t, "exit": trade["sl"], "days": k}
        if hi >= trade["tp"]:
            return {"result": "익절", "exit_date": t, "exit": trade["tp"], "days": k}
        if k >= MAX_HOLD:
            return {"result": "기간만료", "exit_date": t, "exit": cl, "days": k}
    return None


# ───────────── 상태 · 기록 ─────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("alerted", {})
    s.setdefault("open", [])
    s.setdefault("stats", {"n": 0, "wins": 0, "sum": 0.0})
    s.pop("active", None)           # 이전 버전 항목 정리
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def append_trade(row):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["신호일", "마켓", "이름", "기준가", "익절가", "손절가", "청산일", "청산가", "결과", "수익률(%)", "보유일"])
        w.writerow(row)


# ───────────── 텔레그램 ─────────────
def send_telegram(text, dry=False):
    if dry or not (TG_TOKEN and TG_CHAT):
        print("─── 알림 ───\n" + text + "\n────────────", flush=True)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur); cur = ""
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
    if p >= 100000:
        return f"{round(p):,}"
    if p >= 1:
        return f"{p:,.5g}"
    return f"{p:.4g}"


def fmt_won(x):
    return f"{x / 1e8:,.1f}억" if x >= 1e8 else f"{x / 1e4:,.0f}만"


# ───────────── 한 번 실행 ─────────────
def run_once(dry=False):
    t0 = time.monotonic()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    state = load_state()
    markets = krw_markets()
    need = set(markets) | {o["m"] for o in state["open"]}
    log(f"마켓 {len(markets)}개 조회")

    def fetch(m):
        try:
            return m, daily_rows(m, today), None
        except Exception as e:
            return m, None, e

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        data = {m: (rows, err) for m, rows, err in ex.map(fetch, sorted(need))}
    errors = sum(1 for _, e in data.values() if e)

    # 1) 가상 매매 결과 업데이트
    closed_msgs, still_open = [], []
    for o in state["open"]:
        rows, err = data.get(o["m"], (None, None))
        if not rows:
            still_open.append(o)
            continue
        res = resolve(o, rows)
        if not res:
            still_open.append(o)
            continue
        r = res["exit"] / o["entry"] * (1 - FEE) ** 2 - 1
        st = state["stats"]; st["n"] += 1; st["sum"] += r; st["wins"] += 1 if r > 0 else 0
        append_trade([o["date"], o["m"], o["name"], o["entry"], o["tp"], o["sl"], res["exit_date"],
                      res["exit"], res["result"], round(r * 100, 2), res["days"]])
        icon = {"익절": "✅", "손절": "🛑", "기간만료": "⏳"}[res["result"]]
        closed_msgs.append(f"{icon} {res['result']} · {o['name']} ({o['m'][4:]})\n"
                           f"   {fmt_price(o['entry'])} → {fmt_price(res['exit'])}원  {r * 100:+.1f}%  ({res['days']}일 보유)")
    state["open"] = still_open

    # 2) 새 매수 신호
    signals = []
    for m, name in markets.items():
        rows, err = data.get(m, (None, None))
        if not rows or len(rows) < 120:
            continue
        ev = evaluate(rows)
        if ev["signal"] and state["alerted"].get(m) != ev["date"]:
            state["alerted"][m] = ev["date"]
            entry = ev["close"]
            o = {"m": m, "name": name, "date": ev["date"], "entry": entry,
                 "tp": entry * (1 + TP), "sl": entry * (1 - SL)}
            state["open"].append(o)
            signals.append((o, ev))

    # 3) 알림
    if signals:
        lines = [f"🟢 매수 신호 {len(signals)}개 (주봉·일봉 WR ≤ {WR_TH:g} {RUN_DAYS}일↑ 후 첫 상승 마감)", ""]
        for o, ev in signals:
            exp = (datetime.strptime(o["date"], "%Y-%m-%d") + timedelta(days=MAX_HOLD)).strftime("%m/%d")
            lines += [f"• {o['name']} ({o['m'][4:]})",
                      f"   기준가 {fmt_price(o['entry'])}원 ({o['date'][5:]} 종가)",
                      f"   🎯 익절 {fmt_price(o['tp'])}원 (+{TP * 100:g}%)",
                      f"   🛑 손절 {fmt_price(o['sl'])}원 (-{SL * 100:g}%)",
                      f"   ⏳ 최대 {exp}까지 보유",
                      f"   주봉 {ev['wr_w']:.1f} / 일봉 {ev['wr_d']:.1f} · {ev['run_at_signal']}일 연속 후 반등 · 거래대금 {fmt_won(ev['liq'])}/일",
                      ""]
        send_telegram("\n".join(lines).rstrip(), dry)
    if closed_msgs:
        st = state["stats"]
        summary = (f"📊 누적 가상 성적: {st['n']}건 · 승률 {st['wins'] / st['n'] * 100:.0f}% · "
                   f"평균 {st['sum'] / st['n'] * 100:+.1f}%") if st["n"] else ""
        send_telegram("가상 매매 결과\n\n" + "\n".join(closed_msgs) + ("\n\n" + summary if summary else ""), dry)

    # 오래된 기록 정리 + 실행 기록
    cutoff = (datetime.now(KST) - timedelta(days=90)).strftime("%Y-%m-%d")
    state["alerted"] = {m: d for m, d in state["alerted"].items() if d >= cutoff}
    state["last_run"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    save_state(state)
    log(f"신호 {len(signals)}개, 청산 {len(closed_msgs)}개, 보유 중 {len(state['open'])}개, "
        f"조회 실패 {errors}개, {time.monotonic() - t0:.0f}초")


def check(market):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = daily_rows(market, today)
    ev = evaluate(rows)
    f = lambda x: "-" if x is None else f"{x:.1f}"
    print(f"{market} ({ev['date']} 마감 기준)\n"
          f"  종가 {fmt_price(ev['close'])}원 · 주봉 WR {f(ev['wr_w'])} · 일봉 WR {f(ev['wr_d'])}\n"
          f"  둘 다 {WR_TH:g} 이하 연속: {ev['run']}일 (기준 {RUN_DAYS}일)\n"
          f"  20일 평균 거래대금: {fmt_won(ev['liq'])}\n"
          f"  오늘 신호: {'있음' if ev['signal'] else '없음'}")


def main():
    ap = argparse.ArgumentParser(description="빗썸 과매도 반등 신호 알림")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", metavar="MARKET")
    a = ap.parse_args()
    if a.check:
        check(a.check.upper())
        return
    if not a.dry_run and not (TG_TOKEN and TG_CHAT):
        log("텔레그램 설정이 없어 화면 출력 모드로 실행합니다.")
    try:
        run_once(a.dry_run)
    except Exception as e:
        log(f"실행 오류: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
