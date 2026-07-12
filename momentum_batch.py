"""
MomentumScout 日次バッチ v2 (F-01, F-05, F-09, F-11)
J-Quants API (V2) から東証全銘柄の日次OHLCV+財務を取得し、
多期間出来高変化率・過熱度・時価総額・チャートパターン・撤退シグナルを
計算して docs/data.json に出力する。引け後 16:50 JST 実行想定。

必要な環境変数:
  JQUANTS_API_KEY     : J-QuantsのAPIキー (V2。ダッシュボードから発行)
  DISCORD_WEBHOOK_URL : (任意) 日次サマリー/撤退警告の通知先
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE = "https://api.jquants.com/v2"
CACHE_DIR = Path("cache")
OUT_PATH = Path("docs/data.json")
QUOTE_LOOKBACK_DAYS = 70       # 出来高20日平均+余裕
STMT_LOOKBACK_DAYS = 130       # 四半期開示を一巡させ発行済株式数を集める
TOP_N_OUTPUT = 300
MIN_TURNOVER_JPY = 100_000_000
REQ_INTERVAL_SEC = 1.05        # レートリミット Light=60req/分 に収める


def http_json(url: str, headers: dict | None = None, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(20)  # レートリミット(60req/分)の回復待ち
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return {}


def auth_headers() -> dict:
    key = os.environ.get("JQUANTS_API_KEY")
    if not key:
        sys.exit("環境変数 JQUANTS_API_KEY が未設定です "
                 "(V2 APIキー。J-Quantsダッシュボードから発行)")
    return {"x-api-key": key}


def fetch_paginated(url: str, headers: dict) -> list[dict]:
    """V2は全エンドポイントが {"data": [...], "pagination_key": ...} 形式"""
    rows, page = [], None
    while True:
        u = url + (f"&pagination_key={page}" if page else "")
        data = http_json(u, headers)
        rows.extend(data.get("data", []))
        page = data.get("pagination_key")
        if not page:
            return rows
        time.sleep(REQ_INTERVAL_SEC)


def iter_business_days(lookback: int):
    today = datetime.now(JST).date()
    for i in range(lookback, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() < 5:
            yield i, d.strftime("%Y-%m-%d")


def fetch_by_date_cached(endpoint: str, prefix: str,
                         lookback: int, headers: dict) -> list[tuple[str, list]]:
    """日付ループ+ローカルキャッシュ。[(date, rows), ...] を返す"""
    CACHE_DIR.mkdir(exist_ok=True)
    out = []
    for i, ds in iter_business_days(lookback):
        cache = CACHE_DIR / f"{prefix}_{ds}.json"
        if cache.exists() and i > 0:
            rows = json.loads(cache.read_text())
        else:
            rows = fetch_paginated(f"{BASE}{endpoint}?date={ds}", headers)
            if i > 0:
                cache.write_text(json.dumps(rows, ensure_ascii=False))
            time.sleep(REQ_INTERVAL_SEC)
        out.append((ds, rows))
    return out


def fetch_listed(headers: dict) -> dict[str, dict]:
    rows = fetch_paginated(f"{BASE}/equities/master?", headers)
    return {r["Code"][:4]: {"name": r.get("CoName", ""),
                            "market": r.get("MktNm", ""),
                            "sector": r.get("S33Nm", "")} for r in rows}


def fetch_earnings(headers: dict) -> dict[str, str]:
    """今後の決算発表予定: code -> 直近の未来発表日 'YYYY-MM-DD'
    (V2は3月期・9月期決算の翌営業日発表分を返す)"""
    out: dict[str, str] = {}
    today = datetime.now(JST).strftime("%Y-%m-%d")
    try:
        rows = fetch_paginated(f"{BASE}/equities/earnings-calendar?", headers)
    except (urllib.error.URLError, KeyError):
        return out  # 予定APIが落ちても本体を壊さない
    for r in rows:
        d = r.get("Date", "")  # 未定の場合は空文字
        code = (r.get("Code") or "")[:4]
        if code and d >= today:
            if code not in out or d < out[code]:
                out[code] = d
    return out


def fetch_quotes(headers: dict) -> dict[str, list[dict]]:
    by_code: dict[str, list[dict]] = {}
    for _, rows in fetch_by_date_cached("/equities/bars/daily",
                                        "q2", QUOTE_LOOKBACK_DAYS, headers):
        for r in rows:
            if r.get("C") is None or r.get("Vo") is None:
                continue
            by_code.setdefault(r["Code"][:4], []).append({
                "date": r["Date"],
                "open": float(r.get("O") or r["C"]),
                "high": float(r.get("H") or r["C"]),
                "low": float(r.get("L") or r["C"]),
                "close": float(r["C"]),
                "volume": float(r["Vo"]),
                "turnover": float(r.get("Va") or 0),
            })
    return by_code


def fetch_shares(headers: dict) -> dict[str, float]:
    """発行済株式数(自己株含む): 四半期開示を遡って最新値を採用"""
    shares: dict[str, float] = {}
    for _, rows in fetch_by_date_cached("/fins/summary",
                                        "s2", STMT_LOOKBACK_DAYS, headers):
        for r in rows:  # 日付昇順ループなので後の開示で上書き=最新が残る
            v = r.get("ShOutFY")  # 期末発行済株式数
            if v:
                try:
                    shares[r["Code"][:4]] = float(v)
                except (ValueError, TypeError):
                    pass
    return shares


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi14(closes):
    if len(closes) < 15:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes[-15:-1], closes[-14:])]
    losses = [max(a - b, 0) for a, b in zip(closes[-15:-1], closes[-14:])]
    avg_g, avg_l = sum(gains) / 14, sum(losses) / 14
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def heat_score(vr5, dev25, rsi):
    s = min(vr5, 6) / 6 * 40
    if dev25 is not None:
        s += min(max(dev25, 0), 30) / 30 * 35
    if rsi is not None:
        s += max(rsi - 50, 0) / 50 * 25
    return round(s)


def detect_patterns(rows: list[dict]) -> tuple[list[str], list[str]]:
    """(継続/エントリー系パターン, 撤退シグナル) を返す (F-09)"""
    pat, exit_ = [], []
    t, p = rows[-1], rows[-2]
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    body = abs(t["close"] - t["open"])
    rng = max(t["high"] - t["low"], 1e-9)
    is_neg = t["close"] < t["open"]
    vol_max20 = t["volume"] >= max(vols[-20:])

    if t["close"] > max(closes[-21:-1]):
        pat.append("20日高値ブレイク")
    if t["open"] > p["high"] and t["close"] > t["open"]:
        pat.append("窓開け上昇")
    if (t["close"] > t["open"] and p["close"] < p["open"]
            and t["close"] > p["open"] and t["open"] < p["close"]):
        pat.append("包み陽線")

    upper = t["high"] - max(t["open"], t["close"])
    if upper > body * 2 and upper / rng > 0.5 and t["volume"] > 2 * sma(vols[:-1], 5):
        exit_.append("長い上ヒゲ+出来高急増")
    if vol_max20 and is_neg:
        exit_.append("出来高最大の陰線")
    if (t["close"] < t["open"] and p["close"] > p["open"]
            and t["close"] < p["open"] and t["open"] > p["close"]):
        exit_.append("包み陰線")
    v5_now, v5_prev = sma(vols, 3), sma(vols[:-3], 5)
    if v5_prev and v5_now < v5_prev * 0.5 and closes[-1] < closes[-4]:
        exit_.append("出来高急減+失速")
    return pat, exit_


SIGNAL_LOG = CACHE_DIR / "signals_log.jsonl"
SIGNAL_LOG_PERSIST = Path("perf/signals_log.jsonl")  # リポジトリ永続版


def update_performance(all_stocks: list[dict], data_date: str) -> dict:
    """タスク5(F-10): シグナル発生を記録し、翌日/5日後リターンを追記確定。
    直近90日の確定分からシグナル種別ごとの勝率・平均リターンを集計。"""
    CACHE_DIR.mkdir(exist_ok=True)
    close_by_code = {s["code"]: s["close"] for s in all_stocks}

    # 1) 既存ログを読む(cache優先、無ければリポジトリ永続版から復元)
    src = SIGNAL_LOG if SIGNAL_LOG.exists() else SIGNAL_LOG_PERSIST
    records = []
    if src.exists():
        for line in src.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    for rec in records:
        c = close_by_code.get(rec["code"])
        if c is None:
            continue
        d0 = datetime.strptime(rec["date"], "%Y-%m-%d").date()
        dn = datetime.strptime(data_date, "%Y-%m-%d").date()
        bdays = sum(1 for i in range((dn - d0).days)
                    if (d0 + timedelta(days=i + 1)).weekday() < 5)
        base = rec["close"]
        if base:
            if rec.get("r1d") is None and bdays >= 1:
                rec["r1d"] = round((c / base - 1) * 100, 2)
            if rec.get("r5d") is None and bdays >= 5:
                rec["r5d"] = round((c / base - 1) * 100, 2)

    # 2) 本日のシグナル発生を新規追記(上位80銘柄まで)
    logged = {(r["date"], r["code"]) for r in records}
    for s in all_stocks[:80]:
        if (s["exit"] or s["patterns"]) and (data_date, s["code"]) not in logged:
            records.append({"date": data_date, "code": s["code"],
                            "close": s["close"], "signals": s["exit"],
                            "patterns": s["patterns"], "r1d": None, "r5d": None})

    # 3) 90日より古い確定済みは破棄して保存
    cutoff = (datetime.now(JST).date() - timedelta(days=95)).strftime("%Y-%m-%d")
    records = [r for r in records if r["date"] >= cutoff]
    SIGNAL_LOG.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                    for r in records), encoding="utf-8")

    # 4) シグナル種別ごとに集計(撤退は「下落=的中」、パターンは「上昇=的中」)
    stat: dict[str, dict] = {}
    for r in records:
        if r.get("r1d") is None:
            continue
        for sig in r.get("signals", []):
            e = stat.setdefault(sig, {"kind": "exit", "n": 0, "hit": 0, "sum5": 0.0, "n5": 0})
            e["n"] += 1
            if r["r1d"] < 0:
                e["hit"] += 1
            if r.get("r5d") is not None:
                e["sum5"] += r["r5d"]; e["n5"] += 1
        for pat in r.get("patterns", []):
            e = stat.setdefault(pat, {"kind": "entry", "n": 0, "hit": 0, "sum5": 0.0, "n5": 0})
            e["n"] += 1
            if r["r1d"] > 0:
                e["hit"] += 1
            if r.get("r5d") is not None:
                e["sum5"] += r["r5d"]; e["n5"] += 1
    rows = []
    for name, e in stat.items():
        if e["n"] < 3:
            continue
        rows.append({"signal": name, "kind": e["kind"], "n": e["n"],
                     "winRate": round(e["hit"] / e["n"] * 100),
                     "avg5d": round(e["sum5"] / e["n5"], 2) if e["n5"] else None})
    rows.sort(key=lambda x: x["n"], reverse=True)
    return {"window": "直近90日", "signals": rows}


def build(listed, quotes, shares, earnings) -> dict:
    stocks, latest_date = [], ""
    sector_agg: dict[str, dict] = {}  # 業種別集計(取得全銘柄が母数)
    for code, rows in quotes.items():
        if len(rows) < 26:
            continue
        rows.sort(key=lambda r: r["date"])
        closes = [r["close"] for r in rows]
        vols = [r["volume"] for r in rows]
        last = rows[-1]
        latest_date = max(latest_date, last["date"])
        v5b = sma(vols[:-1], 5)
        sector = listed.get(code, {}).get("sector", "")
        # 業種集計は流動性フィルタ前の全銘柄で行う(バイアス防止)
        if sector and v5b and vols[-2]:
            a = sector_agg.setdefault(sector, {"vr5": [], "to": 0.0, "toPrev": 0.0, "n": 0})
            a["vr5"].append(vols[-1] / v5b)
            a["to"] += last["turnover"]
            a["toPrev"] += rows[-2]["turnover"]
            a["n"] += 1
        if last["turnover"] < MIN_TURNOVER_JPY:
            continue
        v5, v20, prev_vol = sma(vols[:-1], 5), sma(vols[:-1], 20), vols[-2]
        if not v5 or not v20 or not prev_vol:
            continue
        c25 = sma(closes, 25)
        dev25 = (closes[-1] / c25 - 1) * 100 if c25 else None
        rsi = rsi14(closes)
        vr5 = vols[-1] / v5
        heat = heat_score(vr5, dev25, rsi)
        pat, exit_ = detect_patterns(rows)
        if heat >= 70:
            exit_.insert(0, "過熱度70+")
        if rsi is not None and rsi >= 80:
            exit_.append("RSI 80+")
        if dev25 is not None and dev25 >= 25:
            exit_.append("25日線乖離+25%超")
        sh = shares.get(code)
        mcap = round(closes[-1] * sh / 1e8) if sh else None  # 億円

        def ret(n):
            return round((closes[-1] / closes[-1 - n] - 1) * 100, 1) if len(closes) > n else None

        rec = {
            "code": code,
            "name": listed.get(code, {}).get("name", code),
            "market": listed.get(code, {}).get("market", ""),
            "sector": sector,
            "vr1": round(vols[-1] / prev_vol, 2),
            "vr5": round(vr5, 2),
            "vr20": round(vols[-1] / v20, 2),
            "r1": ret(1), "r5": ret(5), "r20": ret(20),
            "turnover": round(last["turnover"] / 1e8, 1),
            "mcap": mcap,
            "close": closes[-1],
            "dev25": round(dev25, 1) if dev25 is not None else None,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "heat": heat,
            "patterns": pat,
            "exit": exit_,
            "volHist": vols[-20:],
        }
        if code in earnings:
            rec["earnDate"] = earnings[code]
        stocks.append(rec)
    stocks.sort(key=lambda s: s["vr5"], reverse=True)
    out_stocks = stocks[:TOP_N_OUTPUT]
    add_laggards(out_stocks)  # タスク4(出力銘柄集合内で完結)
    sectors = summarize_sectors(sector_agg)  # タスク3
    perf = update_performance(stocks, latest_date)  # タスク5
    return {"updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
            "dataDate": latest_date, "stocks": out_stocks,
            "sectors": sectors, "performance": perf}


def summarize_sectors(agg: dict) -> list[dict]:
    """タスク3: 業種別の中央値vr5・売買代金前日比・銘柄数"""
    def median(xs):
        xs = sorted(xs)
        n = len(xs)
        return 0 if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)
    out = []
    for name, a in agg.items():
        if a["n"] < 3:
            continue
        to_chg = ((a["to"] / a["toPrev"] - 1) * 100) if a["toPrev"] else 0
        out.append({"name": name, "vr5med": round(median(a["vr5"]), 2),
                    "toChg": round(to_chg), "n": a["n"]})
    out.sort(key=lambda x: x["vr5med"], reverse=True)
    return out


def add_laggards(stocks: list[dict]) -> None:
    """タスク4: vr5上位50の各銘柄に同業種の出遅れ候補(最大5)を付与"""
    by_sector: dict[str, list[dict]] = {}
    for s in stocks:
        if s["sector"]:
            by_sector.setdefault(s["sector"], []).append(s)
    for s in stocks[:50]:
        peers = by_sector.get(s["sector"], [])
        mc = s.get("mcap")
        cand = []
        for p in peers:
            if p["code"] == s["code"] or p["vr5"] >= 1.5:
                continue
            if mc and p.get("mcap"):
                if not (mc * 0.2 <= p["mcap"] <= mc * 5):
                    continue
            cand.append(p)
        cand.sort(key=lambda p: p["turnover"], reverse=True)
        if cand:
            s["laggards"] = [p["code"] for p in cand[:5]]


def notify_discord(payload: dict) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    lines = [f"📊 MomentumScout 日次サマリー ({payload['dataDate']})", ""]
    for s in payload["stocks"][:10]:
        mc = f"{s['mcap']}億円" if s["mcap"] else "—"
        lines.append(f"{s['name']} ({s['code']}) 出来高×{s['vr5']} / "
                     f"{s['r1']:+.1f}% / 時価総額{mc}")
    warns = [s for s in payload["stocks"][:50] if s["exit"]][:8]
    if warns:
        lines += ["", "⚠️ 撤退シグナル検知 (上位50銘柄中)"]
        for s in warns:
            lines.append(f"{s['name']} ({s['code']}): {'・'.join(s['exit'])}")
    soon = [s for s in payload["stocks"][:50] if s.get("earnDate")][:8]
    if soon:
        lines += ["", "📅 決算発表が近い銘柄 (上位50中)"]
        for s in soon:
            lines.append(f"{s['name']} ({s['code']}): {s['earnDate']} 発表予定")
    body = json.dumps({"content": "\n".join(lines)[:1900]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.URLError as e:
        print(f"Discord通知失敗: {e}", file=sys.stderr)


def main() -> None:
    headers = auth_headers()
    print("銘柄マスタ取得中...")
    listed = fetch_listed(headers)
    print(f"{len(listed)}銘柄")
    print("財務(発行済株式数)取得中...")
    shares = fetch_shares(headers)
    print(f"株式数判明: {len(shares)}銘柄")
    print("決算発表予定取得中...")
    earnings = fetch_earnings(headers)
    print(f"予定判明: {len(earnings)}銘柄")
    print("日次データ取得中...")
    quotes = fetch_quotes(headers)
    payload = build(listed, quotes, shares, earnings)
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"出力: {OUT_PATH} ({len(payload['stocks'])}銘柄, {payload['dataDate']})")
    notify_discord(payload)


if __name__ == "__main__":
    main()
