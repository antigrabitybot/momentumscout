#!/usr/bin/env python3
"""買い候補スコアの定時Discord通知。

docs/data.json (momentum_batch.py の出力) からスコア SCORE_MIN 以上の銘柄を
抽出して Discord Webhook へ送信する。データは前日16:50の引け値ベースなので、
朝の場前に通知する運用を想定 (.github/workflows/notify.yml の cron で設定)。

環境変数:
  DISCORD_WEBHOOK_URL  必須。未設定なら何もせず正常終了 (通知はオプション機能)
  SCORE_MIN            通知する最低スコア (既定 5、範囲 3〜7)
"""
import json
import os
import sys
import urllib.request

CHECK_LABELS = ["出来高2〜4×", "パターン", "撤退なし", "過熱度30〜60",
                "代金10億+", "決算遠い", "業種資金流入"]


def main() -> None:
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not hook:
        print("DISCORD_WEBHOOK_URL 未設定のためスキップ (通知はオプション機能)")
        return
    score_min = max(3, min(7, int(os.environ.get("SCORE_MIN", "5"))))

    with open("docs/data.json", encoding="utf-8") as f:
        d = json.load(f)
    cand = [s for s in d.get("stocks", []) if s.get("score", 0) >= score_min]
    cand.sort(key=lambda s: (-s["score"], -s["vr5"]))

    lines = []
    for s in cand[:20]:
        miss = [CHECK_LABELS[i] for i, c in enumerate(s.get("checks", [])) if not c]
        lines.append(
            f"**{s['score']}/7** {s['name']} ({s['code']}) "
            f"出来高×{s['vr5']} / {s['r1']:+.1f}% / 過熱度{s['heat']}"
            + (f" ｜未達: {'・'.join(miss)}" if miss else " ｜全項目クリア🎯"))
    body = (f"📋 **買い候補スコア {score_min}/7以上** — {d.get('dataDate')} 引けデータ\n"
            + ("\n".join(lines) if lines else "本日は該当銘柄がありません")
            + ("\n(他" + str(len(cand) - 20) + "件は省略)" if len(cand) > 20 else "")
            + "\n※AIやルールによる機械的スクリーニングであり投資助言ではありません")

    req = urllib.request.Request(
        hook, data=json.dumps({"content": body[:1900]}).encode(),
        # User-Agent未設定だとDiscord(Cloudflare)がbot判定で403を返すことがあるため明示
        headers={"Content-Type": "application/json", "User-Agent": "MomentumScout/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            res.read()
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode()[:300]
        except Exception:  # noqa: BLE001
            pass
        sys.exit(f"Discord送信失敗 (HTTP {e.code}: {body_txt})。"
                 "DISCORD_WEBHOOK_URL の値が無効か、Webhookが削除された可能性があります。"
                 "Discordで新しいWebhook URLを発行し、"
                 "リポジトリの Settings → Secrets → DISCORD_WEBHOOK_URL を更新してください")
    print(f"通知送信: {len(cand)}件 (スコア{score_min}以上)")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError:
        sys.exit("docs/data.json がありません。先に daily-batch を実行してください")
