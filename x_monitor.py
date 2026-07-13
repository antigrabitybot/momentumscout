"""
MomentumScout X監視 (F-13)
watch_accounts.json に登録したアカウントの新規投稿をポーリングし、
3段階(正規表現→社名辞書→Claude API)で関連銘柄を推定してDiscordへ通知する。
市場時間中は5分間隔、時間外は30分間隔での実行を想定 (cron / GitHub Actions)。

必要な環境変数:
  X_BEARER_TOKEN      : X API Bearer Token (console.x.com で発行)
  DISCORD_WEBHOOK_URL : 通知先
  ANTHROPIC_API_KEY   : (任意) 第3段階の文脈推定に使用

watch_accounts.json の形式:
  [{"handle": "example_account", "tier": "A"}, ...]
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

X_BASE = "https://api.x.com/2"
STATE_PATH = Path("cache/x_state.json")       # handle -> {user_id, since_id, recent_codes}
ACCOUNTS_PATH = Path("watch_accounts.json")
DATA_PATH = Path("docs/data.json")            # momentum_batch.py の出力 (社名辞書に流用)
CONFIDENCE_MIN = 0.7

CODE_RE = re.compile(r"[\$＄\(（]?([1-9][0-9][0-9A-Z][0-9A-Z])[\)）]?")  # 新形式(130A等)対応
INVEST_HINT_RE = re.compile(r"株|銘柄|買|売|上昇|急騰|決算|開示|材料|ストップ高|IR")


def x_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{X_BASE}{path}",
        headers={"Authorization": f"Bearer {os.environ['X_BEARER_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def load_json(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def build_name_dict() -> dict[str, str]:
    """社名(と単純な短縮形) -> 銘柄コード"""
    data = load_json(DATA_PATH, {"stocks": []})
    d = {}
    for s in data["stocks"]:
        name = s["name"]
        d[name] = s["code"]
        short = re.sub(r"(ホールディングス|グループ|HD|株式会社)$", "", name)
        if len(short) >= 3:
            d[short] = s["code"]
    return d


def infer_by_claude(text: str, recent_codes: list[str]) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return []
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "system": (
            "あなたは日本株の銘柄特定エンジンです。入力された投稿から、言及されている"
            "東証上場銘柄を特定してください。出力はJSONのみ。説明文・マークダウンは禁止。"
            '確信が持てない場合は stocks を空配列にしてください。出力形式: '
            '{"stocks":[{"code":"4桁コード","name":"正式社名","confidence":0.0,'
            '"basis":"根拠20字以内"}]}'
        ),
        "messages": [{"role": "user", "content":
            f"投稿: \"{text}\"\n投稿者の過去言及銘柄（参考）: {recent_codes}"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            out = json.loads(res.read().decode())
        raw = out["content"][0]["text"].strip().removeprefix("```json").removesuffix("```")
        return json.loads(raw).get("stocks", [])
    except Exception as e:
        print(f"Claude推定スキップ: {e}", file=sys.stderr)
        return []


def infer_stocks(text: str, name_dict: dict, valid_codes: set,
                 recent_codes: list[str]) -> list[dict]:
    found = []
    for m in CODE_RE.finditer(text):          # ① コード直接
        code = m.group(1)
        if code in valid_codes:
            found.append({"code": code, "confidence": 0.95, "basis": "コード直接記載"})
    for name, code in name_dict.items():       # ② 社名辞書
        if name and name in text and code not in [f["code"] for f in found]:
            found.append({"code": code, "confidence": 0.85, "basis": f"社名一致:{name}"})
    if not found and INVEST_HINT_RE.search(text):   # ③ 文脈推定
        for s in infer_by_claude(text, recent_codes):
            if s.get("code") in valid_codes and s.get("confidence", 0) >= CONFIDENCE_MIN:
                found.append(s)
    return found


def notify(handle: str, tier: str, text: str, stocks: list[dict],
           stock_map: dict) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    lines = []
    for st in stocks:
        info = stock_map.get(st["code"], {})
        name = info.get("name", st["code"])
        extra = ""
        if info:
            extra = (f"\n出来高×{info.get('vr5', '-')} / 本日{(info.get('r1') or 0):+.1f}% / "
                     f"話題度{info.get('heat', '-')}")
        lines.append(
            f"🔔 X検知: {name} ({st['code']})\n"
            f"発信: @{handle}（Tier {tier}）\n"
            f"「{text[:30]}…」{extra}\n"
            f"https://www.google.com/finance/quote/{st['code']}:TYO"
        )
    body = json.dumps({"content": "\n\n".join(lines)[:1900]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        # User-Agent未設定だとDiscord(Cloudflare)がbot判定で403を返すことがあるため明示
        headers={"Content-Type": "application/json", "User-Agent": "MomentumScout/1.0"})
    urllib.request.urlopen(req, timeout=30)


def main() -> None:
    accounts = load_json(ACCOUNTS_PATH, [])
    if not accounts:
        sys.exit("watch_accounts.json にアカウントを登録してください")
    state = load_json(STATE_PATH, {})
    data = load_json(DATA_PATH, {"stocks": []})
    stock_map = {s["code"]: s for s in data["stocks"]}
    name_dict = build_name_dict()
    valid_codes = set(name_dict.values()) | set(stock_map)

    for acct in accounts:
        h = acct["handle"]
        st = state.setdefault(h, {})
        try:
            if "user_id" not in st:  # 初回のみ ($0.010)
                st["user_id"] = x_get(f"/users/by/username/{h}")["data"]["id"]
            q = f"/users/{st['user_id']}/tweets?max_results=20&exclude=retweets,replies"
            if st.get("since_id"):
                q += f"&since_id={st['since_id']}"
            res = x_get(q)
        except Exception as e:  # noqa: BLE001  1アカウントの失敗で全体を止めない
            print(f"@{h} 取得失敗 ({e})", file=sys.stderr)
            continue
        posts = res.get("data", [])
        if posts:
            st["since_id"] = posts[0]["id"]
        for p in reversed(posts):
            stocks = infer_stocks(p["text"], name_dict, valid_codes,
                                  st.get("recent_codes", []))
            if stocks:
                codes = [s["code"] for s in stocks]
                st["recent_codes"] = (codes + st.get("recent_codes", []))[:10]
                notify(h, acct.get("tier", "-"), p["text"], stocks, stock_map)
                print(f"@{h} -> {codes}")

    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
