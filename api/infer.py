"""
MomentumScout 推論API (タスク6・機能B)
POST /api/infer : 出来高急増の原因をオンデマンド推論する Vercel Serverless Function。

処理フロー (handoff/02 §6-1):
  1. Vercel KVキャッシュ確認 (key=infer:{code}:{spikeDate}, TTL 24h)
  2. X recent search (失敗時は空配列で縮退)
  3. TDnet直近7日の開示 (失敗時は空配列で縮退)
  4. Anthropic API claude-sonnet-4-6 + web_search(max 2回)。
     システムプロンプトは handoff/01 §2 を一字一句そのまま使用 (変更禁止)
  5. スキーマ検証 (不一致なら1回だけ再試行) → KV保存 → 返却

必要な環境変数 (Vercel ダッシュボード → Settings → Environment Variables):
  ANTHROPIC_API_KEY : 必須
  ALLOWED_ORIGIN    : CORS許可オリジン (GitHub PagesのURL)。未設定時のみ * (開発用)
  X_BEARER_TOKEN    : (任意) X検索。無ければSNS証拠なしで続行
  KV_REST_API_URL / KV_REST_API_TOKEN : (任意) Vercel KV。無ければキャッシュ・
                      レート制限なしで続行 (課金防止のため本番では設定必須)
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

JST = timezone(timedelta(hours=9))
MODEL = os.environ.get("INFER_MODEL", "claude-sonnet-4-6")  # handoff/03 運用パラメータ
MAX_TOKENS = 700
WEB_SEARCH_MAX_USES = 2
CACHE_TTL_SEC = 24 * 3600
RATE_IP_PER_HOUR = 10       # handoff/02: IPごと10回/時
RATE_GLOBAL_PER_DAY = 100   # handoff/02: 全体100回/日の安全弁

# handoff/01_fable_inference_design.md §2 確定版 (一字一句変更禁止)
SYSTEM_PROMPT = """あなたは日本株の出来高急増の原因を分析するアナリストです。
ユーザーメッセージで渡される evidence パック（JSON）と、必要に応じて
web_searchツールの結果のみを根拠に、出来高急増の原因仮説を作成してください。

## 厳守ルール
- evidenceとweb_search結果に存在しない事実を書かない。あなたの記憶にある
  当該企業の知識は、検索で裏が取れない限り根拠に使わない
- 各材料の日時を必ず確認し、volume_spike_date より後に発生した材料は
  原因候補から除外する
- SNS投稿は「話題化の証拠」であって「事実の証拠」ではない。SNS発の情報は
  必ず「真偽未確認」と明記する
- 十分な証拠がなければ primary_cause を "材料不明" とし、
  note に「需給・仕掛け・思惑の可能性」と記す。無理に理由を作らない
- 出力は下記JSONのみ。マークダウン・説明文・投資推奨は一切出力しない

## 確信度の校正基準
- "高": 一次情報（適時開示・公式発表）が存在し、日時が急増と整合し、
        報道またはSNS反応が同方向
- "中": 報道またはSNS話題化はあるが一次情報が確認できない
- "低": 間接的な示唆のみ（業種全体の連れ高、類似銘柄の動きなど）
- 材料不明の場合、confidence は必ず "低"

## 出力スキーマ
{
 "primary_cause": "1文・50字以内（例: 7/2引け後の上方修正開示を受けた買い）",
 "confidence": "高|中|低",
 "sources": [{"type":"IR|News|SNS","date":"YYYY-MM-DD","title":"…","url":"…"}],
 "secondary": "副次要因があれば1文、なければ null",
 "sns_heat": "SNSでの話題化状況を1文（真偽未確認と明記）、投稿ゼロなら null",
 "note": "時系列の注意点や不確実性があれば1文、なければ null"
}"""


# ---------------- Vercel KV (Upstash REST 互換) ----------------

def kv_cmd(*args):
    """Redisコマンドを1つ実行。KV未設定・失敗時は None (機能縮退)"""
    url = os.environ.get("KV_REST_API_URL")
    tok = os.environ.get("KV_REST_API_TOKEN")
    if not url or not tok:
        return None
    req = urllib.request.Request(
        url, data=json.dumps(list(args)).encode(), method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode()).get("result")
    except Exception as e:  # noqa: BLE001  KV障害で本体を止めない
        print(f"KV縮退: {e}", file=sys.stderr)
        return None


def cache_get(key: str):
    raw = kv_cmd("GET", key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def cache_set(key: str, value: dict) -> None:
    kv_cmd("SET", key, json.dumps(value, ensure_ascii=False),
           "EX", str(CACHE_TTL_SEC))


def rate_incr(key: str, ttl: int):
    """カウンタを+1して現在値を返す。KV未設定なら None (制限なしで続行)"""
    n = kv_cmd("INCR", key)
    if n == 1:
        kv_cmd("EXPIRE", key, str(ttl))
    return n


# ---------------- evidence 収集 (失敗時は空配列で縮退) ----------------

def fetch_x_posts(name: str, code: str) -> list[dict]:
    """X recent search: 直近48hの言及投稿 (handoff/02 §6-1 手順2)"""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return []
    query = f'("{name}" OR {code}) lang:ja -is:retweet'
    start = (datetime.now(timezone.utc) - timedelta(hours=48)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = ("https://api.x.com/2/tweets/search/recent?"
           + urllib.parse.urlencode({
               "query": query, "max_results": 20, "start_time": start,
               "tweet.fields": "created_at,public_metrics"}))
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            rows = json.loads(res.read().decode()).get("data", [])
    except Exception as e:  # noqa: BLE001
        print(f"X検索縮退: {e}", file=sys.stderr)
        return []
    out = []
    for r in rows:
        met = r.get("public_metrics", {})
        at = r.get("created_at", "")
        try:  # UTC→JST表記に揃える (時系列判定を誤らせない)
            dt = datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ") \
                if "." in at else datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
            at = dt.replace(tzinfo=timezone.utc).astimezone(JST) \
                .strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
        out.append({"at": at, "text": r.get("text", "")[:280],
                    "likes": met.get("like_count", 0),
                    "reposts": met.get("retweet_count", 0)})
    return out


def fetch_ir_recent7d(code: str) -> list[dict]:
    """TDnet直近7日の開示 (handoff/02 §6-1 手順3)。
    TDnetに公式JSON APIが無いため公開プロキシ(yanoshin webapi)を利用。
    一次ソースの適時開示情報そのものを返す。失敗時は空配列で縮退し、
    web_search側でモデルが補完的に裏取りする。"""
    url = f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit=30"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MomentumScout/1.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            items = json.loads(res.read().decode()).get("items", [])
    except Exception as e:  # noqa: BLE001
        print(f"TDnet縮退: {e}", file=sys.stderr)
        return []
    limit = (datetime.now(JST) - timedelta(days=7)).strftime("%Y-%m-%d")
    out = []
    for it in items:
        t = it.get("Tdnet", {})
        pub = (t.get("pubdate") or "")[:16]
        if pub >= limit:
            out.append({"date": pub, "title": t.get("title", ""),
                        "url": t.get("document_url", "")})
    return out[:10]


def build_evidence(body: dict, ir: list, x_posts: list) -> dict:
    """handoff/01 §3 の evidenceパック構造 (変更禁止)"""
    return {
        "stock": {"code": body["code"], "name": body["name"],
                  "sector": body.get("sector", "")},
        "volume_spike_date": body["spikeDate"],
        "stats": {"vr5": body.get("vr5"), "r1": body.get("r1"),
                  "r5": body.get("r5")},
        "ir_recent7d": ir,
        "x_posts_48h": x_posts,
        "instruction": ("web_searchは最大2回まで。"
                        "「社名 + 急騰/材料/ニュース」で直近の報道を確認せよ"),
    }


# ---------------- Anthropic API 呼び出し ----------------

def extract_json(text: str) -> dict:
    """モデル出力からJSONオブジェクトを抽出 (コードフェンス等を許容)"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSONが見つからない")
    return json.loads(m.group(0))


def validate_result(o) -> str | None:
    """handoff/01 §2 出力スキーマの検証。問題なければ None"""
    if not isinstance(o, dict):
        return "オブジェクトでない"
    if not isinstance(o.get("primary_cause"), str) or not o["primary_cause"]:
        return "primary_cause が不正"
    if o.get("confidence") not in ("高", "中", "低"):
        return "confidence が不正"
    if o["primary_cause"] == "材料不明" and o["confidence"] != "低":
        return "材料不明なのに confidence が低でない"
    if not isinstance(o.get("sources"), list):
        return "sources が不正"
    for s in o["sources"]:
        if not isinstance(s, dict) or s.get("type") not in ("IR", "News", "SNS", "不明"):
            return "sources[].type が不正"
    for k in ("secondary", "sns_heat", "note"):
        if o.get(k) is not None and not isinstance(o[k], str):
            return f"{k} が不正"
    return None


def anthropic_call(evidence: dict, api_key: str) -> dict:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": [{"type": "web_search_20250305", "name": "web_search",
                   "max_uses": WEB_SEARCH_MAX_USES}],
        "messages": [{"role": "user",
                      "content": json.dumps(evidence, ensure_ascii=False)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    # Vercel maxDuration=60s に対し web_search 2回分の余裕を見る
    with urllib.request.urlopen(req, timeout=50) as res:
        out = json.loads(res.read().decode())
    text = "".join(b.get("text", "") for b in out.get("content", [])
                   if b.get("type") == "text")
    return extract_json(text)


def call_claude(evidence: dict, api_key: str) -> dict:
    """スキーマ不一致なら1回だけ再試行 (handoff/02 §6-1 手順5)"""
    last_err = ""
    for _ in range(2):
        try:
            result = anthropic_call(evidence, api_key)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = f"出力パース失敗: {e}"
            continue
        err = validate_result(result)
        if err is None:
            return result
        last_err = f"スキーマ不一致: {err}"
    raise ValueError(last_err)


# ---------------- HTTPハンドラ (Vercel Python Functions) ----------------

class handler(BaseHTTPRequestHandler):  # noqa: N801  Vercel規約のクラス名

    def _cors_headers(self) -> dict:
        allowed = os.environ.get("ALLOWED_ORIGIN", "")
        origin = self.headers.get("Origin", "")
        # ALLOWED_ORIGIN設定時はそのオリジンのみ許可。未設定は開発用に * (本番では必ず設定)
        value = allowed if (allowed and origin == allowed) else ("*" if not allowed else "")
        h = {"Access-Control-Allow-Methods": "POST, OPTIONS",
             "Access-Control-Allow-Headers": "Content-Type"}
        if value:
            h["Access-Control-Allow-Origin"] = value
        return h

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for k, v in self._cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._handle()
        except Exception as e:  # noqa: BLE001  想定外は500で返しログに残す
            print(f"infer失敗: {e}", file=sys.stderr)
            self._send(500, {"error": "推論に失敗しました。時間をおいて再試行してください"})

    def _handle(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._send(500, {"error": "サーバー設定エラー (ANTHROPIC_API_KEY 未設定)"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode())
        except (ValueError, json.JSONDecodeError) as e:
            if os.environ.get("INFER_DEBUG"):
                self._send(400, {"error": "JSONボディが不正です", "debug": {
                    "exc": str(e), "content_length_header": self.headers.get("Content-Length"),
                    "headers": dict(self.headers), "raw_len": len(raw) if "raw" in dir() else None,
                    "raw_head": (raw[:200].decode(errors="replace") if "raw" in dir() else None)}})
                return
            self._send(400, {"error": "JSONボディが不正です"})
            return

        code = str(body.get("code", ""))
        name = str(body.get("name", ""))
        spike = str(body.get("spikeDate", ""))
        # 東証4桁コード。新形式は2・4文字目に英字が入り得る (例: 130A)
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", code) or not name \
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", spike):
            self._send(400, {"error": "code / name / spikeDate が不正です"})
            return
        body["code"], body["name"], body["spikeDate"] = code, name, spike

        # レート制限 (KV利用可能時のみ)。全体上限→IP上限の順に判定
        today = datetime.now(JST).strftime("%Y%m%d")
        hour = datetime.now(JST).strftime("%Y%m%d%H")
        ip = (self.headers.get("x-forwarded-for", "?").split(",")[0].strip())
        n_global = rate_incr(f"rl:global:{today}", 25 * 3600)
        if n_global is not None and n_global > RATE_GLOBAL_PER_DAY:
            self._send(429, {"error": "本日の全体利用上限に達しました"})
            return
        n_ip = rate_incr(f"rl:ip:{ip}:{hour}", 3600)
        if n_ip is not None and n_ip > RATE_IP_PER_HOUR:
            self._send(429, {"error": "時間あたりの利用上限に達しました"})
            return

        # キャッシュ (同一銘柄・同日は再課金しない)
        cache_key = f"infer:{code}:{spike}"
        cached = cache_get(cache_key)
        if cached:
            cached["cached"] = True
            self._send(200, cached)
            return

        ir = fetch_ir_recent7d(code)
        x_posts = fetch_x_posts(name, code)
        evidence = build_evidence(body, ir, x_posts)
        result = call_claude(evidence, api_key)
        result["cached"] = False
        cache_set(cache_key, result)
        self._send(200, result)
