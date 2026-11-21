#!/usr/bin/env python3
"""
빗썸 KRW 코인 '과매도 반등' 신호 알림 + 가상 매매 기록 봇

[기본 전략 — 꺼져 있음. 켜려면 환경변수 WR_STRATEGY=1]
  주봉 WR(14)과 일봉 WR(14)이 둘 다 -85 이하인 날이 14일 이상 이어진 뒤,
  처음으로 일봉이 상승 마감(전일 종가보다 높게 마감)한 날
  + 최근 20일 평균 하루 거래대금 1억원 이상
  → 그날 종가를 기준가로 알림

[매도 기준 (가상 매매)]
  익절 +15% / 손절 -10% / 최대 30일 보유 (같은 날 둘 다 닿으면 손절로 처리)

[전략 ① 급락 코인 90일선 → 60일선 (가상 기록)]
  최근 90일 안에 30일 저점의 2배 이상 급등한 날이 있고,
  그 고점보다 70% 이상 낮은 가격에서 위에서 내려와 90일선에 닿으면 90일선 가격에 매수
  (같은 급등 고점에서는 첫 번째 터치만), 20일 평균 거래대금 1억 이상
  → 60일선에 닿으면 매도 (60일선이 매수가보다 위일 때만), 손절 없음, 최대 60일 보유
  매일 자정 직후 '오늘 매수 대기 목록'(90일선 지정가, 60일선 목표가)과 가상 체결·청산을 알림
  장중(--intraday, 매시간): 대기 코인이 지정가에 닿으면 바로 '체결(가상)' 알림,
  보유 코인이 그날 목표가에 닿으면 바로 '목표 도달' 알림

[전략 ② 꼬리·급락 반등 (가상 기록, 가장 강조해서 알림)]
  최근 30일 안에, 14일 사이 큰 윗꼬리(고가가 30일선보다 5%↑, 윗꼬리 10%↑) 2번 이상
  → 그 구간 최고 종가보다 25% 이상 급락 → 종가가 7일선을 아래에서 위로 돌파한 날
  + 90일선 대비 70% 이하(이격도 ≤70), 7일선 위 2.5% 이내, 20일 평균 거래대금 1억~5억
  → 그날 종가에 매수, +15%에 지정가 매도, 14일 안에 안 닿으면 14일째 종가에 매도
  백테스트(빗썸 2020~2026) 68건 · 승률 85% · 평균 +10.2%

[일목 기준선 매도 알림 (KIJUN_WATCH 코인, 기본 KRW-HUMA)]
  기준선 = 최근 26일(오늘 포함) 최고가와 최저가의 중간
  매일 자정 직후 오늘의 기준선(매도 지정가)과 앞으로 바뀔 날짜를 알리고, 장중 고가가 닿으면 바로 알림

백테스트(빗썸 2020 ~ 2026-11)에서 고른 조건이며, 미래 수익을 보장하지 않습니다.
외부 패키지 없이 파이썬 표준 라이브러리만 사용 (Python 3.9+)

사용법
  python3 wr_alert.py                 # 신호 확인 + 가상 매매 결과 업데이트 (하루 1번, 자정 직후 실행)
  python3 wr_alert.py --dry-run       # 텔레그램 대신 화면 출력
  python3 wr_alert.py --check KRW-BTC # 특정 코인의 현재 상태 확인
  python3 wr_alert.py --intraday      # 장중 확인 (90일선 전략 지정가 체결·목표 도달)
"""
import argparse
import csv
import html
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
# 전략 ① 급락 코인 90일선 → 60일선
A_ON = os.getenv("WR_STRATEGY", "0") == "1"     # 기본 WR 전략 (백테스트 성적이 약해 기본값 꺼짐)
B_ON = os.getenv("MA_STRATEGY", "1") != "0"
B_BUY_MA, B_SELL_MA = 90, 60
B_PUMP = 2.0          # 30일 저점 대비 고가 배수 (2배 = +100%)
B_LOOKBACK = 90       # 급등 고점을 찾는 기간
B_DROP = 0.70         # 급등 고점 대비 하락폭
B_MAX_HOLD = 60
B_MIN_AGE = 125       # 상장 후 최소 일수
# 전략 ② 꼬리·급락 반등
C_ON = os.getenv("WICK_STRATEGY", "1") != "0"
C_TP, C_HOLD = 0.15, 14
C_MAX_LIQ = 5e8
KIJUN_WATCH = [m.strip().upper() for m in os.getenv("KIJUN_WATCH", "KRW-HUMA").split(",") if m.strip()]
C_NOTE = "백테스트(빗썸 2020~2026) 68건 · 승률 85% · 평균 +10.2% · 평균 7.6일 보유"
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
    """마감된 일봉만, 오래된 순. 각 행: (날짜, 고가, 저가, 종가, 거래대금, 시가)"""
    data = api_get("/candles/days", {"market": market, "count": CANDLES})
    rows = {}
    for c in data:
        t = c["candle_date_time_kst"][:10]
        if t >= today:          # 진행 중인 오늘 봉은 제외
            continue
        rows[t] = (t, float(c["high_price"]), float(c["low_price"]),
                   float(c["trade_price"]), float(c["candle_acc_trade_price"]), float(c["opening_price"]))
    return [rows[k] for k in sorted(rows)]


def today_candle(market, today):
    """진행 중인 오늘 일봉 (시가, 고가, 저가, 현재가). 없으면 None"""
    data = api_get("/candles/days", {"market": market, "count": 1})
    if not data or data[0]["candle_date_time_kst"][:10] != today:
        return None
    c = data[0]
    return float(c["opening_price"]), float(c["high_price"]), float(c["low_price"]), float(c["trade_price"])


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
    for k, r in enumerate(after, start=1):
        t, hi, lo, cl = r[0], r[1], r[2], r[3]
        if lo <= trade["sl"]:
            return {"result": "손절", "exit_date": t, "exit": trade["sl"], "days": k}
        if hi >= trade["tp"]:
            return {"result": "익절", "exit_date": t, "exit": trade["tp"], "days": k}
        if k >= MAX_HOLD:
            return {"result": "기간만료", "exit_date": t, "exit": cl, "days": k}
    return None


# ───────────── 전략 ①: 급락 코인 90일선 매수 → 60일선 매도 ─────────────
def ma_before(c, p, i):
    """i일 시작 시점에 알 수 있는 이동평균 = i일 이전 p개 종가 평균"""
    return sum(c[i - p:i]) / p if i >= p else None


def b_peak(h, l, i):
    """i일 기준 직전 90일 안의 급등 고점 위치 (30일 저점의 2배 이상인 날 중 고가가 가장 높은 날)"""
    pk, ph = -1, 0.0
    for p in range(max(30, i - B_LOOKBACK), i):
        if h[p] >= B_PUMP * min(l[p - 30:p]) and h[p] > ph:
            ph, pk = h[p], p
    return pk


def b_scan(rows):
    """창 안의 모든 신호(백테스트와 같은 순서·중복 규칙)와 마지막으로 쓴 급등 고점"""
    n = len(rows)
    h = [r[1] for r in rows]; l = [r[2] for r in rows]; c = [r[3] for r in rows]
    v = [r[4] for r in rows]; o = [r[5] for r in rows]
    start = 91 if n >= CANDLES else B_MIN_AGE
    sigs, used = [], -1
    for i in range(start, n):
        m, mp = ma_before(c, B_BUY_MA, i), ma_before(c, B_BUY_MA, i - 1)
        if not (c[i - 1] > mp and l[i] <= m):
            continue
        pk = b_peak(h, l, i)
        if pk < 0:
            continue
        peak_high = max(h[pk:i])
        entry = min(o[i], m)
        if entry > peak_high * (1 - B_DROP):
            continue
        if sum(v[i - 20:i]) / 20 < MIN_LIQ:
            continue
        if pk == used:
            continue
        used = pk
        tgt = ma_before(c, B_SELL_MA, i)
        sigs.append({"i": i, "date": rows[i][0], "entry": entry, "ma90": m, "tgt": tgt,
                     "peak": peak_high, "peak_date": rows[pk][0], "valid": tgt is not None and tgt > entry * 1.02})
    return sigs, used


def b_watch(rows, used):
    """오늘(마지막 마감일 다음 날) 90일선에 닿으면 신호가 되는지와 지정가·목표가"""
    n = len(rows)
    if n < CANDLES and n < B_MIN_AGE:
        return None
    h = [r[1] for r in rows]; l = [r[2] for r in rows]; c = [r[3] for r in rows]; v = [r[4] for r in rows]
    mp, level = ma_before(c, B_BUY_MA, n - 1), ma_before(c, B_BUY_MA, n)
    if mp is None or not c[n - 1] > mp:
        return None
    pk = b_peak(h, l, n)
    if pk < 0 or pk == used:
        return None
    peak_high = max(h[pk:n])
    if level > peak_high * (1 - B_DROP) or sum(v[n - 20:n]) / 20 < MIN_LIQ:
        return None
    tgt = ma_before(c, B_SELL_MA, n)
    if tgt is None or tgt <= level * 1.02:
        return None
    return {"level": level, "tgt": tgt, "close": c[n - 1], "peak": peak_high, "peak_date": rows[pk][0]}


def b_resolve(trade, rows):
    """매수 다음 날부터: 60일선(그날 시작 시점 값)이 매수가보다 위이고 고가가 닿으면 매도, 60일째는 종가"""
    idx = {r[0]: k for k, r in enumerate(rows)}
    i = idx.get(trade["date"])
    if i is None:
        return None
    c = [r[3] for r in rows]
    for j in range(i + 1, min(len(rows), i + B_MAX_HOLD + 1)):
        tg = ma_before(c, B_SELL_MA, j)
        if tg is not None and tg > trade["entry"] and rows[j][1] >= tg:
            return {"result": "60일선 도달", "exit_date": rows[j][0], "exit": max(rows[j][5], tg), "days": j - i, "tgt": tg}
        if j == i + B_MAX_HOLD:
            return {"result": "기간만료", "exit_date": rows[j][0], "exit": rows[j][3], "days": B_MAX_HOLD, "tgt": tg}
    return None


def b_today_target(rows):
    c = [r[3] for r in rows]
    return ma_before(c, B_SELL_MA, len(rows))


# ───────────── 전략 ②: 꼬리·급락 반등 ─────────────
def ma_incl(c, p, i):
    """차트와 같은 이동평균: i일 종가까지 포함한 p일 평균"""
    return sum(c[i - p + 1:i + 1]) / p if i >= p - 1 else None


def c_scan(rows):
    """창 안의 후보(이격도 ≤90)를 백테스트와 같은 순서·중복 규칙으로 찾고, 최종 조건을 통과했는지 표시"""
    n = len(rows)
    h = [r[1] for r in rows]; l = [r[2] for r in rows]; c = [r[3] for r in rows]
    v = [r[4] for r in rows]; o = [r[5] for r in rows]
    spike = [False] * n
    for d in range(90, n):
        spike[d] = h[d] >= ma_incl(c, 30, d) * 1.05 and h[d] >= max(o[d], c[d]) * 1.10
    sigs, last_e, busy = [], -1, -1
    for i in range(100, n):
        if i <= busy:
            continue
        e = -1
        for k in range(i - 1, i - 31, -1):
            if spike[k] and sum(spike[k - 13:k + 1]) >= 2:
                e = k
                break
        if e < 0 or e == last_e:
            continue
        cmax = max(c[e - 13:e + 1])
        if not min(l[e + 1:i + 1]) <= cmax * 0.75:
            continue
        m7p, m7 = ma_incl(c, 7, i - 1), ma_incl(c, 7, i)
        if not (c[i - 1] < m7p and c[i] > m7):
            continue
        liq = sum(v[i - 20:i]) / 20
        if liq < MIN_LIQ:
            continue
        ratio = c[i] / ma_incl(c, 90, i)
        if ratio > 0.9:
            continue
        last_e = e
        gap7 = c[i] / m7 - 1
        sigs.append({"i": i, "date": rows[i][0], "entry": c[i], "ratio": ratio, "gap7": gap7, "liq": liq,
                     "drop": min(l[e + 1:i + 1]) / cmax - 1, "valid": ratio <= 0.7 and gap7 <= 0.025 and liq <= C_MAX_LIQ})
        busy = i + 10
    return sigs


def c_resolve(trade, rows):
    """매수 다음 날부터 14일: 고가가 +15%에 닿으면 그 가격(시가가 더 높으면 시가), 아니면 14일째 종가"""
    idx = {r[0]: k for k, r in enumerate(rows)}
    i = idx.get(trade["date"])
    if i is None:
        return None
    tp = trade["entry"] * (1 + C_TP)
    for k in range(C_HOLD):
        j = i + 1 + k
        if j >= len(rows):
            return None
        if rows[j][1] >= tp:
            return {"result": "+15% 도달", "exit_date": rows[j][0], "exit": max(rows[j][5], tp), "days": k + 1}
        if k == C_HOLD - 1:
            return {"result": "14일 만료", "exit_date": rows[j][0], "exit": rows[j][3], "days": C_HOLD}
    return None


def run_strategy_c(state, markets, data, dry):
    """전략 ②: 가상 청산 → 새 신호(가장 강조) → 보유 현황. (신호 수, 청산 수) 반환"""
    st = state["c_stats"]
    closed, still = [], []
    for o in state["c_open"]:
        rows, _ = data.get(o["m"], (None, None))
        res = c_resolve(o, rows) if rows else None
        if not res:
            still.append(o)
            continue
        r = res["exit"] / o["entry"] * (1 - FEE) ** 2 - 1
        st["n"] += 1; st["sum"] += r; st["wins"] += 1 if r > 0 else 0
        append_trade([o["date"], o["m"], o["name"], o["entry"], round(o["entry"] * (1 + C_TP), 8), "없음",
                      res["exit_date"], round(res["exit"], 8), "[꼬리반등] " + res["result"], round(r * 100, 2), res["days"]])
        icon = "✅" if r > 0 else "🔻"
        closed.append(f"{icon} {res['result']} · {o['name']} ({o['m'][4:]})\n"
                      f"   {fmt_price(o['entry'])} → {fmt_price(res['exit'])}원  {r * 100:+.1f}%  ({res['days']}일 보유)")
    state["c_open"] = still

    held = {o["m"] for o in state["c_open"]}
    sigs_new, last_day = [], None
    for m, name in markets.items():
        rows, _ = data.get(m, (None, None))
        if not rows or len(rows) < 101:
            continue
        last_day = last_day or rows[-1][0]
        sigs = c_scan(rows)
        s = sigs[-1] if sigs else None
        if s and s["valid"] and s["i"] == len(rows) - 1 and state["c_alerted"].get(m) != s["date"] and m not in held:
            state["c_alerted"][m] = s["date"]
            o = {"m": m, "name": name, "date": s["date"], "entry": s["entry"]}
            state["c_open"].append(o); held.add(m)
            sigs_new.append((o, s))
    state["c_init"] = True

    if sigs_new:
        e = html.escape
        lines = ["🚨🚨🚨 <b>최우선 매수 신호</b> 🚨🚨🚨",
                 f"<b>[꼬리·급락 반등] {len(sigs_new)}개</b>",
                 f"<b>{e(C_NOTE)}</b>", ""]
        for o, s in sigs_new:
            exp = (datetime.strptime(o["date"], "%Y-%m-%d") + timedelta(days=C_HOLD)).strftime("%m/%d")
            lines += [f"🔥 <b>{e(o['name'])} ({e(o['m'][4:])})</b>",
                      f"   💰 매수가 <b>{fmt_price(o['entry'])}원</b> ({o['date'][5:]} 종가 · 지금 매수)",
                      f"   🎯 매도 지정가 <b>{fmt_price(o['entry'] * (1 + C_TP))}원 (+15%)</b> · 매수 직후 걸어두기",
                      f"   ⏳ {exp}까지 안 닿으면 그날 종가에 정리 · 손절 없음",
                      "   ⚖️ 권장 비중: <b>자산의 1/3</b> (백테스트 기준)",
                      f"   이격도 {s['ratio'] * 100:.0f} · 7일선 위 {s['gap7'] * 100:.1f}% · 급락 {s['drop'] * 100:.0f}% · 거래대금 {fmt_won(s['liq'])}/일",
                      ""]
        lines.append("⚠️ 승률 85%는 과거 기록이에요. 진 거래는 평균 -13%였어요(최악 -23%).")
        send_telegram("\n".join(lines).strip(), dry, as_html=True)

    msg = []
    daily = last_day is not None and state.get("c_daily") != last_day
    if daily:
        state["c_daily"] = last_day
    if state["c_open"] and daily:
        msg.append("📌 [꼬리반등] 가상 보유 중 (+15% 지정가 대기)")
        for o in state["c_open"]:
            rows, _ = data.get(o["m"], (None, None))
            if not rows:
                continue
            cl = rows[-1][3]
            left = C_HOLD - (datetime.strptime(rows[-1][0], "%Y-%m-%d") - datetime.strptime(o["date"], "%Y-%m-%d")).days
            msg.append(f"• {o['name']} ({o['m'][4:]}) {o['date'][5:]} 매수 {fmt_price(o['entry'])} · 목표 {fmt_price(o['entry'] * (1 + C_TP))}"
                       f" · 어제 종가 {fmt_price(cl)} ({(cl / o['entry'] - 1) * 100:+.0f}%) · 남은 {left}일"
                       + (" · 이전 신호 이어받음" if o.get("backfill") else ""))
    if closed:
        msg += ["", "[꼬리반등] 가상 매매 결과", ""] + closed
        msg.append(f"📊 [꼬리반등] 누적: {st['n']}건 · 승률 {st['wins'] / st['n'] * 100:.0f}% · 평균 {st['sum'] / st['n'] * 100:+.1f}%")
    if msg:
        send_telegram("\n".join(msg).strip(), dry)
    return len(sigs_new), len(closed)


# ───────────── 일목 기준선 매도 알림 ─────────────
def kijun(highs, lows):
    return (max(highs) + min(lows)) / 2


def kijun_plan(rows, days=14):
    """마감된 일봉으로 오늘과 이후 기준선 (새 고점·저점이 안 나온다고 가정). [(날짜, 값)]"""
    H = [r[1] for r in rows]; L = [r[2] for r in rows]
    d0 = datetime.strptime(rows[-1][0], "%Y-%m-%d")
    out = []
    for k in range(0, days + 1):          # k=0: 오늘(마지막 마감일 다음 날), 창 = 마감된 최근 25-k일
        n = 25 - k
        if n <= 0:
            break
        out.append(((d0 + timedelta(days=k + 1)).strftime("%Y-%m-%d"), kijun(H[-n:], L[-n:])))
    return out


def run_kijun(state, markets, data, dry):
    lines = []
    for m in KIJUN_WATCH:
        rows, _ = data.get(m, (None, None))
        if not rows or len(rows) < 26:
            continue
        plan = kijun_plan(rows)
        day, kj = plan[0]
        if state["k_daily"].get(m) == day:
            continue
        state["k_daily"][m] = day
        prev = kijun([r[1] for r in rows[-26:]], [r[2] for r in rows[-26:]])
        cl = rows[-1][3]
        steps, last = [], kj
        for dte, val in plan[1:]:
            if abs(val / last - 1) >= 0.005:
                steps.append(f"{dte[5:]} {fmt_price(val)}")
                last = val
            if len(steps) >= 5:
                break
        name = markets.get(m, m[4:])
        lines += [f"📍 [기준선 매도] {name} ({m[4:]}) · {day[5:]}",
                  f"   오늘 기준선 {fmt_price(kj)}원 (어제 {fmt_price(prev)}원, {(kj / prev - 1) * 100:+.1f}%)",
                  f"   어제 종가 {fmt_price(cl)}원 → 기준선까지 {(kj / cl - 1) * 100:+.0f}%",
                  "   다음 계단(새 고점·저점 없을 때): " + (" · ".join(steps) if steps else "당분간 그대로"), ""]
    if lines:
        send_telegram("\n".join(lines).strip(), dry)


def check_kijun_intraday(today, rt, dry):
    """장중: 오늘 고가가 기준선(오늘 봉 포함 26일)에 닿으면 하루 한 번 알림. 보낸 수 반환"""
    msgs = []
    for m in KIJUN_WATCH:
        if rt.get("kj:" + m) == today:
            continue
        try:
            data = api_get("/candles/days", {"market": m, "count": 30})
        except Exception as e:
            log(f"기준선 조회 실패 {m}: {e}")
            continue
        days = sorted(data, key=lambda x: x["candle_date_time_kst"])
        closed = [x for x in days if x["candle_date_time_kst"][:10] < today][-25:]
        td = [x for x in days if x["candle_date_time_kst"][:10] == today]
        if not td or len(closed) < 25:
            continue
        t0 = td[0]
        kj = kijun([float(x["high_price"]) for x in closed] + [float(t0["high_price"])],
                   [float(x["low_price"]) for x in closed] + [float(t0["low_price"])])
        if float(t0["high_price"]) >= kj:
            rt["kj:" + m] = today
            msgs.append(f"• {m[4:]} 오늘 고가 {fmt_price(float(t0['high_price']))}원 ≥ 기준선 {fmt_price(kj)}원 · 지금 {fmt_price(float(t0['trade_price']))}원")
    if msgs:
        send_telegram("🔔 [기준선 도달] 매도 기준 가격에 닿았어요\n\n" + "\n".join(msgs), dry)
    return len(msgs)


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
    s.setdefault("b_open", [])
    s.setdefault("b_alerted", {})
    s.setdefault("b_stats", {"n": 0, "wins": 0, "sum": 0.0})
    s.setdefault("b_watch", {})
    s.setdefault("c_open", [])
    s.setdefault("c_alerted", {})
    s.setdefault("c_stats", {"n": 0, "wins": 0, "sum": 0.0})
    s.setdefault("k_daily", {})
    s.setdefault("b_rt", {})          # 장중 알림 보낸 기록 {"fill:KRW-X": 날짜, "tp:KRW-X": 날짜}
    s.pop("active", None)           # 이전 버전 항목 정리
    s["b_open"] = [o for o in s["b_open"] if not o.get("backfill")]   # 이어받은 과거 신호는 기록하지 않음
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
def send_telegram(text, dry=False, as_html=False):
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
        body = {"chat_id": TG_CHAT, "text": chunk}
        if as_html:
            body["parse_mode"] = "HTML"
        data = urllib.parse.urlencode(body).encode()
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
    need = set(markets) | {o["m"] for o in state["open"]} | {o["m"] for o in state["b_open"]} | {o["m"] for o in state["c_open"]}
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
    for m, name in (markets.items() if A_ON else []):
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

    # 0) 전략 ② (가장 먼저 · 가장 강조)
    c_sig, c_closed = (run_strategy_c(state, markets, data, dry) if C_ON else (0, 0))

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

    # 4) 전략 ①
    b_sig, b_closed = (run_strategy_b(state, markets, data, dry) if B_ON else (0, 0))

    # 5) 일목 기준선 매도 알림
    run_kijun(state, markets, data, dry)

    # 오래된 기록 정리 + 실행 기록
    cutoff = (datetime.now(KST) - timedelta(days=90)).strftime("%Y-%m-%d")
    state["alerted"] = {m: d for m, d in state["alerted"].items() if d >= cutoff}
    state["b_alerted"] = {m: d for m, d in state["b_alerted"].items() if d >= cutoff}
    state["c_alerted"] = {m: d for m, d in state["c_alerted"].items() if d >= cutoff}
    state["last_run"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    save_state(state)
    log(f"신호 {len(signals)}개, 청산 {len(closed_msgs)}개, 보유 중 {len(state['open'])}개 | "
        f"① 체결 {b_sig}개, 청산 {b_closed}개, 보유 중 {len(state['b_open'])}개 | "
        f"② 신호 {c_sig}개, 청산 {c_closed}개, 보유 중 {len(state['c_open'])}개 | "
        f"조회 실패 {errors}개, {time.monotonic() - t0:.0f}초")


def run_strategy_b(state, markets, data, dry):
    """전략 ①: 가상 청산 → 가상 체결 → 매수 대기 목록. (체결 수, 청산 수) 반환"""
    first = not state.get("b_init")
    st = state["b_stats"]

    # 1) 가상 보유 청산
    closed, still = [], []
    for o in state["b_open"]:
        rows, _ = data.get(o["m"], (None, None))
        res = b_resolve(o, rows) if rows else None
        if not res:
            still.append(o)
            continue
        r = res["exit"] / o["entry"] * (1 - FEE) ** 2 - 1
        st["n"] += 1; st["sum"] += r; st["wins"] += 1 if r > 0 else 0
        append_trade([o["date"], o["m"], o["name"], round(o["entry"], 8), round(res["tgt"], 8) if res["tgt"] else "",
                      "없음", res["exit_date"], round(res["exit"], 8), "[90일선] " + res["result"],
                      round(r * 100, 2), res["days"]])
        icon = "✅" if res["result"] == "60일선 도달" else "⏳"
        closed.append(f"{icon} {res['result']} · {o['name']} ({o['m'][4:]})\n"
                      f"   {fmt_price(o['entry'])} → {fmt_price(res['exit'])}원  {r * 100:+.1f}%  ({res['days']}일 보유)")
    state["b_open"] = still

    # 2) 어제 체결된 신호 + 매수 대기 목록
    held = {o["m"] for o in state["b_open"]}
    fills, watch, backfill = [], [], []
    last_day = None
    for m, name in markets.items():
        rows, _ = data.get(m, (None, None))
        if not rows or len(rows) < B_MIN_AGE:
            continue
        sigs, used = b_scan(rows)
        last_day = last_day or rows[-1][0]
        if first:   # 처음 실행: 최근 60일 안에 생겨 아직 끝나지 않은 신호를 이어서 기록
            for s in sigs:
                if s["valid"] and s["i"] >= len(rows) - B_MAX_HOLD and s["i"] < len(rows) - 1 and m not in held:
                    o = {"m": m, "name": name, "date": s["date"], "entry": s["entry"], "backfill": True}
                    if not b_resolve(o, rows):
                        state["b_open"].append(o); held.add(m); backfill.append(o)
        s = sigs[-1] if sigs else None
        if s and s["i"] == len(rows) - 1 and s["valid"] and state["b_alerted"].get(m) != s["date"] and m not in held:
            state["b_alerted"][m] = s["date"]
            o = {"m": m, "name": name, "date": s["date"], "entry": s["entry"]}
            state["b_open"].append(o); held.add(m)
            fills.append((o, s))
        if m not in held:
            w = b_watch(rows, used)
            if w:
                watch.append((m, name, w))
    state["b_init"] = True

    # 오늘 장중 확인용 목록 저장 + 보유 코인의 오늘 목표가 갱신
    day = (datetime.strptime(last_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") if last_day else None
    state["b_watch"] = {m: {"name": name, "level": w["level"], "tgt": w["tgt"], "day": day} for m, name, w in watch}
    for o in state["b_open"]:
        rows, _ = data.get(o["m"], (None, None))
        if rows and o.get("day") != day:
            o["tgt_prev"] = o.get("tgt_today")
            o["tgt_today"], o["day"] = b_today_target(rows), day

    # 3) 알림 (장중에 이미 체결 알림을 보낸 코인은 생략)
    fills = [(o, s) for o, s in fills if state["b_rt"].get("fill:" + o["m"]) != s["date"]]
    if fills:
        lines = [f"🔵 [90일선] 가상 체결 {len(fills)}개 ({fills[0][1]['date'][5:]} 장중 90일선 터치)", ""]
        for o, s in fills:
            lines += [f"• {o['name']} ({o['m'][4:]})",
                      f"   매수가 {fmt_price(o['entry'])}원 (90일선)",
                      f"   🎯 목표 60일선 {fmt_price(s['tgt'])}원 ({(s['tgt'] / o['entry'] - 1) * 100:+.0f}%, 매일 바뀜)",
                      f"   급등 고점 {fmt_price(s['peak'])}원 ({s['peak_date'][5:]}) 대비 {(o['entry'] / s['peak'] - 1) * 100:.0f}%",
                      f"   손절 없음 · 최대 {B_MAX_HOLD}일 보유", ""]
        send_telegram("\n".join(lines).rstrip(), dry)

    msg = []
    daily = last_day is not None and state.get("b_daily") != last_day   # 대기·보유 목록은 하루 한 번만
    if daily:
        state["b_daily"] = last_day
    if watch and daily:
        watch.sort(key=lambda x: x[2]["close"] / x[2]["level"])
        day = (datetime.strptime(last_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%m/%d") if last_day else "오늘"
        msg += [f"🔔 [90일선] {day} 매수 대기 {len(watch)}개 (90일선 지정가 → 60일선 목표)", ""]
        for m, name, w in watch:
            msg += [f"• {name} ({m[4:]})",
                    f"   매수 지정가 {fmt_price(w['level'])}원 (현재가 대비 {(w['level'] / w['close'] - 1) * 100:.0f}%)",
                    f"   🎯 목표 60일선 {fmt_price(w['tgt'])}원 ({(w['tgt'] / w['level'] - 1) * 100:+.0f}%)",
                    f"   급등 고점 {fmt_price(w['peak'])}원 ({w['peak_date'][5:]}) 대비 {(w['level'] / w['peak'] - 1) * 100:.0f}%", ""]
    if state["b_open"] and daily:
        msg += ["📌 [90일선] 가상 보유 중 · 오늘 매도 목표가 (60일선)"]
        for o in state["b_open"]:
            rows, _ = data.get(o["m"], (None, None))
            if not rows:
                continue
            tg, cl = b_today_target(rows), rows[-1][3]
            days = (datetime.strptime(rows[-1][0], "%Y-%m-%d") - datetime.strptime(o["date"], "%Y-%m-%d")).days
            prev = o.get("tgt_prev")
            chg = f" (전날 {fmt_price(prev)}에서 {(tg / prev - 1) * 100:+.1f}%)" if prev else ""
            note = "목표가가 매수가 아래라 아직 매도 안 함" if tg <= o["entry"] else f"매수가 대비 {(tg / o['entry'] - 1) * 100:+.0f}%"
            msg += [f"• {o['name']} ({o['m'][4:]}) · {o['date'][5:]} 매수 {fmt_price(o['entry'])} · {days}/{B_MAX_HOLD}일"
                    + (" · 이전 신호 이어받음" if o.get("backfill") else ""),
                    f"   🎯 오늘 목표 {fmt_price(tg)}원{chg} · {note}",
                    f"   어제 종가 {fmt_price(cl)} ({(cl / o['entry'] - 1) * 100:+.0f}%)"]
    if closed:
        msg += ["", "[90일선] 가상 매매 결과", ""] + closed
        msg.append(f"📊 [90일선] 누적: {st['n']}건 · 승률 {st['wins'] / st['n'] * 100:.0f}% · 평균 {st['sum'] / st['n'] * 100:+.1f}%")
    if msg:
        send_telegram("\n".join(msg).strip(), dry)
    return len(fills), len(closed)


def run_intraday(dry=False):
    """장중: 대기 코인의 지정가 체결, 보유 코인의 오늘 목표가 도달을 바로 알림 (가상)"""
    t0 = time.monotonic()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    state = load_state()
    rt = state["b_rt"]
    watch = {m: w for m, w in state["b_watch"].items() if w.get("day") == today and rt.get("fill:" + m) != today}
    held = [o for o in state["b_open"] if o.get("day") == today and o.get("tgt_today")
            and o["tgt_today"] > o["entry"] and rt.get("tp:" + o["m"]) != today and o["date"] < today]
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    held_c = [o for o in state["c_open"] if o["date"] < today and rt.get("tp2:" + o["m"]) != today
              and (today_dt - datetime.strptime(o["date"], "%Y-%m-%d")).days <= C_HOLD]
    need = sorted(set(watch) | {o["m"] for o in held} | {o["m"] for o in held_c})
    kj_sent = check_kijun_intraday(today, rt, dry)
    if not need:
        if kj_sent:
            save_state(state)
        log(f"장중 확인할 코인 없음 · 기준선 알림 {kj_sent}개")
        return
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        cand = dict(zip(need, ex.map(lambda m: _safe(today_candle, m, today), need)))
    fills, tps = [], []
    for m, w in watch.items():
        k = cand.get(m)
        if k and k[2] <= w["level"]:
            entry = min(k[0], w["level"])
            rt["fill:" + m] = today
            fills.append(f"• {w['name']} ({m[4:]})\n"
                         f"   매수가 {fmt_price(entry)}원 (90일선 {fmt_price(w['level'])}) · 지금 {fmt_price(k[3])}원\n"
                         f"   🎯 목표 60일선 {fmt_price(w['tgt'])}원 ({(w['tgt'] / entry - 1) * 100:+.0f}%, 내일부터 매일 바뀜)")
    for o in held:
        k = cand.get(o["m"])
        if k and k[1] >= o["tgt_today"]:
            px = max(k[0], o["tgt_today"])
            r = px / o["entry"] * (1 - FEE) ** 2 - 1
            rt["tp:" + o["m"]] = today
            tps.append(f"• {o['name']} ({o['m'][4:]}) {fmt_price(o['entry'])} → {fmt_price(px)}원  {r * 100:+.1f}% · 지금 {fmt_price(k[3])}원")
    tps_c = []
    for o in held_c:
        k = cand.get(o["m"])
        tp = o["entry"] * (1 + C_TP)
        if k and k[1] >= tp:
            px = max(k[0], tp)
            r = px / o["entry"] * (1 - FEE) ** 2 - 1
            rt["tp2:" + o["m"]] = today
            tps_c.append(f"• {o['name']} ({o['m'][4:]}) {fmt_price(o['entry'])} → {fmt_price(px)}원  {r * 100:+.1f}% · 지금 {fmt_price(k[3])}원")
    if tps_c:
        send_telegram(f"✅ [꼬리반등] +15% 목표 도달 (가상) {len(tps_c)}개\n\n" + "\n".join(tps_c)
                      + "\n\n확정 결과와 누적 성적은 자정에", dry)
    if fills:
        send_telegram(f"🔵 [90일선] 지정가 체결 (가상) {len(fills)}개\n\n" + "\n".join(fills)
                      + "\n\n손절 없음 · 최대 60일 보유 · 확정 기록은 자정에", dry)
    if tps:
        send_telegram(f"✅ [90일선] 목표가 도달 (가상) {len(tps)}개\n\n" + "\n".join(tps)
                      + "\n\n확정 결과와 누적 성적은 자정에", dry)
    cutoff = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d")
    state["b_rt"] = {k: d for k, d in rt.items() if d >= cutoff}
    save_state(state)
    log(f"장중: 대기 {len(watch)}개·보유 {len(held)}개·꼬리반등 {len(held_c)}개 확인, 체결 {len(fills)}개, "
        f"목표 도달 {len(tps)}개, 꼬리반등 도달 {len(tps_c)}개, {time.monotonic() - t0:.0f}초")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log(f"조회 실패 {a[0]}: {e}")
        return None


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
    sigs, used = b_scan(rows)
    w = b_watch(rows, used)
    print(f"[90일선 전략] 창 안 신호: " + (", ".join(f"{s['date']} {fmt_price(s['entry'])}{'' if s['valid'] else '(목표 미달)'}" for s in sigs) or "없음"))
    print(f"  오늘 매수 대기: " + (f"지정가 {fmt_price(w['level'])} → 목표 {fmt_price(w['tgt'])}" if w else "아님"))
    cs = c_scan(rows) if len(rows) >= 101 else []
    print("[꼬리반등] 창 안 후보: " + (", ".join(f"{s['date']} {fmt_price(s['entry'])}{' ✅신호' if s['valid'] else ''}" for s in cs) or "없음"))


def main():
    ap = argparse.ArgumentParser(description="빗썸 과매도 반등 신호 알림")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", metavar="MARKET")
    ap.add_argument("--intraday", action="store_true")
    a = ap.parse_args()
    if a.check:
        check(a.check.upper())
        return
    if not a.dry_run and not (TG_TOKEN and TG_CHAT):
        log("텔레그램 설정이 없어 화면 출력 모드로 실행합니다.")
    try:
        if a.intraday:
            run_intraday(a.dry_run)
        else:
            run_once(a.dry_run)
    except Exception as e:
        log(f"실행 오류: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
