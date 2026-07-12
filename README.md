# MomentumScout

東証全銘柄の多期間出来高スクリーニング + スマホ向けガラスUIダッシュボード + X監視通知。
仕様書 v1.0 の Phase 1 (F-01/02/04/05/11) + F-09 (パターン検知・撤退支援) + F-13 実装。

## 主な機能

- 多期間出来高スクリーナー (前日/5日/20日、倍率・売買代金・**時価総額帯**で絞り込み)
- **業種表示** (東証33業種。リスト行・詳細シートに表示)
- **決算発表日の警告バッジ** (本日/明日/N営業日後決算を⚠赤ピルで表示)
- **セクター資金マップ** (33業種のvr5中央値ヒートマップ。タップで業種絞り込み)
- **連れ高候補** (詳細シートに同業種の出遅れ銘柄を最大5件)
- **シグナル成績記録 (F-10)** (各シグナルの翌日勝率・5日リターンを90日集計。設定タブ)
- バブルマップ (出来高×過熱度、タップで銘柄詳細)
- 銘柄詳細シート: 騰落・出来高倍率・時価総額・過熱度・出来高スパークライン
  + **チャートパターン** (高値ブレイク/窓開け/包み線など) + **撤退シグナル**
  (上ヒゲ+出来高急増/出来高最大陰線/過熱度70+/RSI80+ など)
  + 外部チャート連携 (Google Finance / TradingView / 株探)
- ウォッチタブ: 撤退シグナルが出た保有・監視銘柄を最上段に赤で集約
- 通知: ①バッチからの日次サマリー+撤退警告 (Discord)
  ②アプリ起動時にウォッチ銘柄の撤退シグナルをDiscord+端末通知 (同日1回)
- **推論ボタン (機能B)** (詳細シートから出来高急増の原因をオンデマンドAI分析。
  TDnet開示+X投稿+web_searchを証拠に、確信度付きの原因仮説と出典を表示。
  Vercelデプロイが必要・任意機能)
- 詳細シートは ✕ボタン / 下スワイプ / 背景タップ / **戻るボタン(履歴連動)** のどれでも閉じられます

## 構成

```
momentum_batch.py          日次バッチ: J-Quants → 指標計算 → docs/data.json + Discord通知
x_monitor.py               F-13: Xアカウント監視 → 銘柄推定 → Discord通知
docs/index.html            スマホ用アプリ本体 (ビルド不要・1ファイル・PWA)
docs/data.json             バッチの出力 (index.htmlが読む)
.github/workflows/daily.yml  平日16:50 JSTに自動実行
watch_accounts.json        F-13の監視アカウント定義
api/infer.py               機能B: 推論API (Vercel Serverless Function)
vercel.json                Vercelのランタイム設定
requirements.txt           Vercel Python Functionsの依存解決用 (中身は空)
tests/test_infer.py        推論APIの受入テスト (敵対的テストT1〜T7含む)
```

## セットアップ (所要 約15分)

### 1. J-Quants
1. https://jpx-jquants.com/ でライトプラン以上を契約
2. ダッシュボードから **APIキー** を発行 (V2 API。旧リフレッシュトークン方式は廃止)

### 2. GitHubリポジトリ
1. このフォルダをそのままGitHubのプライベート…ではなく**パブリックリポジトリ**にpush
   (GitHub Pagesの無料利用のため。株価派生データのみ公開されるが問題ない範囲。
   非公開にしたい場合はVPSでcron実行 + Basic認証配信に変更)
2. Settings → Pages → Source: `main` / `docs` フォルダを指定
3. Settings → Secrets and variables → Actions に登録:
   - `JQUANTS_API_KEY` (J-Quantsダッシュボードで発行したAPIキー)
   - `DISCORD_WEBHOOK_URL` (Discordのサーバー設定 → 連携サービス → ウェブフックで発行)
4. Actionsタブ → daily-batch → Run workflow で初回手動実行
5. `https://<ユーザー名>.github.io/<リポジトリ名>/` をスマホで開く
   → 共有メニューから**ホーム画面に追加**するとアプリとして起動

### 3. X監視 (F-13, 任意)
1. https://console.x.com でBearer Token発行、$5チャージ、支出上限$20/月を設定
2. `watch_accounts.json` に監視アカウントを記入
3. 環境変数 `X_BEARER_TOKEN` / `DISCORD_WEBHOOK_URL` / (任意)`ANTHROPIC_API_KEY` を設定し
   `python x_monitor.py` をcronで実行 (市場時間5分毎、時間外30分毎)
   GitHub Actionsのscheduleは分単位の精度が低いため、X監視はVPSやRaspberry Pi推奨

### 4. 推論ボタン (機能B, 任意)

出来高急増の原因をワンタップでAI分析する機能。使わない場合はこの節をスキップしてOK。

1. https://vercel.com でアカウント作成 → GitHub連携 → このリポジトリをImport
   (Root Directoryはリポジトリルートのまま。`docs/`はGitHub Pagesが従来通り配信し、
   Vercel側は `/api` だけが実体として使われる)
2. Settings → Environment Variables に登録:
   - `ANTHROPIC_API_KEY` (必須)
   - `ALLOWED_ORIGIN` (必須。GitHub PagesのURL。例 `https://<ユーザー名>.github.io`)
   - `X_BEARER_TOKEN` (任意。無ければSNS証拠なしで動作)
3. Marketplace → **Vercel KV** (Upstash) を追加 (無料枠)。
   `KV_REST_API_URL` / `KV_REST_API_TOKEN` が自動登録される
   (未設定でも動くがキャッシュ・レート制限が効かず再課金されるため本番では必須)
4. デプロイ後のURL `https://<project>.vercel.app/api/infer` を
   アプリの設定タブ「推論API」に貼る
5. リリース前に受入テストを実行し全合格を確認:
   ```
   pip install pytest
   ANTHROPIC_API_KEY=sk-... pytest tests/ -s
   ```
   (敵対的テストT1〜T7で7回推論するため100〜200円程度かかる。
   キー未設定時は無料の単体テストのみ実行される)

注意: VercelのHobbyプラン (無料) は商用利用不可規約。自分専用利用の間は問題ない。

## 日々の使い方

- **17時前後**: Discordに日次サマリーが届く → アプリのホームで候補確認
- **ホーム**: 期間セグメント(前日/5日/20日)を切替えてバブルマップと上位30件を見る
- **行タップ**: 詳細シート → 騰落・出来高倍率・過熱度・RSI・出来高スパークライン → Google Finance
- **★**: ウォッチ登録。過熱度70以上は赤バッジで降り時警告
- **スクリーナー**: 倍率・売買代金の閾値を自分の戦略に合わせて調整 (自動保存)

## 運用コスト目安

| 項目 | 月額 |
|---|---|
| J-Quants ライト | 約1,650円 |
| GitHub Actions/Pages | 無料 |
| X API (20アカウント監視) | 数百〜2,000円程度 |
| Claude API (文脈推定) | 100円未満 |
| 推論ボタン (機能B) | 1回15〜25円 × 使用回数 (同日同銘柄は再課金なし) |

## 注意

- 本ツールは情報整理の補助であり投資判断はすべて自己責任
- **時価総額**は直近四半期開示の発行済株式数(自己株含む)×終値の概算。
  初回実行では過去130日分の開示から収集するため、開示が範囲外の一部銘柄は「—」表示に
  なる (キャッシュが溜まる運用2〜3ヶ月でほぼ全銘柄が埋まる)
- 過熱度スコア・撤退シグナルの閾値は暫定値 (momentum_batch.py)。
  実トレードの記録と突き合わせて調整すること
- 第三者への提供は投資助言・代理業 (金商法) に抵触しうるため自分専用で使うこと

