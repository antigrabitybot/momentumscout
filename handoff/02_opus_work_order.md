# [Opus指示書] MomentumScout 拡張実装 v1.0

実装担当: Claude Opus 4.8（Claude Codeで実行を想定）
前提リポジトリ: momentumscout（momentum_batch.py / docs/index.html / daily.yml）
設計書: handoff/01_fable_inference_design.md（プロンプト・原則は変更禁止）

## タスク1: 業種の付与（機能A・キーワード無し版）

- momentum_batch.py の fetch_listed で `Sector33CodeName` を取得し、
  各銘柄の出力に `"sector": "機械"` を追加
- UI: リスト行の rmeta 先頭に業種を薄字で表示（例「機械 ・ 出来高 ×4.1 …」）、
  詳細シートの社名下にも表示

## タスク2: 決算発表日の警告バッジ

- J-Quants `/fins/announcement` で今後の決算発表予定を取得（日付ループ不要、
  一括エンドポイント。仕様は公式docsで最新を確認すること）
- 各銘柄に `"earnDate": "2026-07-04"` を付与（直近の未来日のみ、無ければ省略）
- UI: earnDateが当日→「⚠今日決算」、翌営業日→「⚠明日決算」の赤ピルを
  リスト行と詳細シートに表示。詳細シートでは日付も併記
- バッチのDiscord日次サマリーにも「明日決算のウォッチ銘柄」節を追加

## タスク3: セクター資金マップ

- バッチで33業種ごとに集計: 中央値vr5、売買代金合計の前日比、構成銘柄数
  → data.json に `"sectors": [{"name":"機械","vr5med":1.8,"toChg":+34,"n":12},…]`
  （集計母数は出力300銘柄ではなく**取得した全銘柄**とすること。バイアス防止）
- UI: ホームのバブルマップ下に折りたたみ式のヒートマップグリッド
  （33業種をタイル表示、色=vr5中央値の強弱、タップでその業種のみに
   ホームリストを絞り込み）。ガラスUIの既存トーンを踏襲、赤青の意味論を維持

## タスク4: 連れ高候補（業種ベース）

- バッチ: vr5上位50銘柄それぞれについて、同一33業種内で
  「vr5 < 1.5 かつ 時価総額が当該銘柄の0.2〜5倍」の銘柄を最大5件抽出し
  `"laggards": ["6501","7012",…]` として付与
- UI: 詳細シートの下部に「同業種の出遅れ候補」セクション。各候補は
  タップで詳細シートを差し替え表示（history連動は既存実装を流用）
- 注記文をUIに含める: 「業種一致に基づく機械的抽出であり、
  事業内容の類似性は保証されません」

## タスク5: F-10 アラート成績の自動記録

- バッチが毎日 cache/signals_log.jsonl に追記:
  {date, code, signals:[…], patterns:[…], close}
- 翌日以降のバッチ実行時、未確定レコードに r1d/r5d（シグナル発生翌日/5日後の
  終値リターン）を追記して確定させる
- 直近90日の確定レコードから、シグナル種別ごとに
  {発生数, 翌日勝率, 5日平均リターン} を集計し data.json の
  `"performance"` に格納
- UI: 設定タブに「シグナル成績（直近90日）」カードを追加し表形式で表示。
  撤退シグナルは「発生後に下がった率」を勝率として表示（向きに注意）
- GitHub Actionsのcacheにsignals_log.jsonlが確実に永続するよう、
  daily.ymlのキャッシュキー設計を見直すこと（消えると成績が失われる）

## タスク6: 推論ボタン（機能B・オンデマンド）

**インフラ: Cloudflare WorkerではなくVercel Serverless Functionを採用**
（GitHubリポジトリ連携で自動デプロイ、Pythonが使えてバッチとロジック共有しやすい、
既存のGitHub Pages/Actions構成に最小変更で乗る）。

### 6-1. Vercel Serverless Function（新規 api/ ディレクトリ、Python）

- リポジトリ直下に以下を追加し、Vercelに同一GitHubリポジトリを連携する
  （Vercelダッシュボード→Add New Project→リポジトリ選択のみでOK。
  `docs/`はGitHub Pagesが従来通り配信し、Vercel側は`/api`だけ生きる）
  ```
  api/
    infer.py           # POST /api/infer のハンドラ本体
  requirements.txt      # Vercel Python Functionsの依存解決に必要
  vercel.json           # ルーティング設定（下記）
  ```
- `vercel.json` 例:
  ```json
  {
    "functions": { "api/infer.py": { "runtime": "python3.12" } },
    "headers": [{
      "source": "/api/(.*)",
      "headers": [
        {"key": "Access-Control-Allow-Origin", "value": "$ALLOWED_ORIGIN"},
        {"key": "Access-Control-Allow-Methods", "value": "POST, OPTIONS"}
      ]
    }]
  }
  ```
  （$ALLOWED_ORIGINはビルド時展開されないため、実装時はinfer.py内で
  os.environ["ALLOWED_ORIGIN"]と比較しヘッダーを動的設定する方式に置き換えること）

- 環境変数（Vercelダッシュボード → Settings → Environment Variables）:
  `X_BEARER_TOKEN`, `ANTHROPIC_API_KEY`, `ALLOWED_ORIGIN`,
  `KV_REST_API_URL` / `KV_REST_API_TOKEN`（Vercel KV、Marketplace経由で無料枠追加）

- POST /api/infer  body: {code, name, sector, spikeDate, vr5, r1, r5}
- 処理:
  1. Vercel KVでキャッシュ確認（key=`${code}:${spikeDate}`、TTL 24h）→ あれば即返す
  2. X recent search: GET /2/tweets/search/recent
     query=`("${name}" OR ${code}) lang:ja -is:retweet`、max_results=20、
     tweet.fields=created_at,public_metrics。失敗しても続行（空配列）
  3. TDnet直近7日: 銘柄コードで開示一覧を取得（実装時に取得手段の現状を
     調査し、一次ソース優先・失敗時は空配列で縮退）
  4. Anthropic API: model=claude-sonnet-4-6、web_searchツール有効(max_uses:2)、
     systemは設計書§2を**一字一句そのまま**使用、userに§3のevidenceパック
  5. 応答JSONをパース検証（スキーマ不一致なら1回だけ再試行）→ KV保存 → 返却
- CORS: ALLOWED_ORIGINのみ許可（GitHub Pagesのオリジン）。簡易レート制限
  （KVでIPごとのカウンタ、10回/時）
- 1日あたりの全体上限（例: 100回）をKVカウンタで管理し、超過時は429を返す安全弁
- Vercel HobbyプランはServerless Functionsの実行時間上限が短いため
  （実装時に最新の上限値を確認）、web_searchの待ち時間を考慮しタイムアウトを
  余裕を持って設計すること。上限に触れる場合はEdge Functionへの切替も検討

### 6-2. アプリ側
- 詳細シートに「🔍 出来高増加の理由を推論」ボタン
- 設定タブにAPI URLの入力欄（localStorage保存、Alchemyキーと同パターン。
  例: `https://<project>.vercel.app/api/infer`）
- 押下→ローディング→推論カード表示:
  主因（confidenceピル: 高=緑/中=黄/低=グレー）、副因、SNS話題化、
  出典リスト（各行タップでURLへ）、note
- primary_cause="材料不明" の場合は専用の落ち着いた表示
  （「明確な材料は確認できませんでした（需給・思惑の可能性）」）にし、
  失敗表示と混同させない
- 同日同銘柄の結果はアプリ側でもlocalStorageにキャッシュ

### 6-3. デプロイ手順（Hirokiさん側の作業）
1. Vercelアカウント作成 → GitHub連携 → momentumscoutリポジトリをImport
2. Root DirectoryはリポジトリルートのままでOK（`docs/`はPagesが別途配信するため
   Vercel側のビルド出力設定は空でよく、`/api`だけが実体として使われる）
3. Settings → Environment Variables に4つのキーを登録
4. Marketplace → Vercel KV を追加（無料枠）
5. デプロイ後に払い出されるURLを設定タブに貼る
6. VercelはHobbyプラン（無料）が商用利用不可規約である点に注意。
   自分専用利用の間は問題ない

## 受入条件（全タスク共通）

- 設計書§4の敵対的テストT1〜T7をモック evidenceで実行し全合格
- 既存機能（スクリーナー/ウォッチ/通知/戻る操作）の回帰確認
- バッチは新規APIが落ちても既存出力を壊さない（縮退動作）
- 完了後、推論結果サンプルをFableの監査（設計書§5）に回すこと
