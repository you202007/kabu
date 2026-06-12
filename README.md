# Nikkei 225 SQ Distortion Dashboard

日経225のSQ前後に発生しやすい指数の歪みを、寄与度、オプション建玉、先物ベーシス、TOPIX乖離から日次でスコア化する試作プロジェクトです。

## Outputs

- `outputs/sq_scores.csv`: 日次スコア
- `outputs/sq_contributions.csv`: 銘柄別寄与度
- `outputs/sq_latest_signal.json`: 最新日の判定
- `outputs/sq_distortion_dashboard_generated.html`: 自動生成ダッシュボード

GitHub Pagesでは `outputs/index.html` を公開します。

## Local Run

```powershell
& 'C:\Users\eveap\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' work\sq_distortion_model.py --generate-sample
```

一般的なPython環境では以下で実行できます。

```bash
pip install -r requirements.txt
python work/sq_distortion_model.py --generate-sample
```

実データを使う場合は、`work/input_data` にCSVを置いて実行します。

```bash
python work/sq_distortion_model.py --input-dir work/input_data --output-dir outputs
```

入力CSVの仕様は `outputs/sq_data_schema.md` を参照してください。

## GitHub Pages

`.github/workflows/pages.yml` により、`main` ブランチへpushされるたびに以下を実行します。

1. Python依存関係をインストール
2. サンプルデータでスコアとダッシュボードを生成
3. `outputs/sq_distortion_dashboard_generated.html` を `outputs/index.html` にコピー
4. `outputs` ディレクトリをGitHub Pagesへデプロイ

実データ接続後は、Workflowの `--generate-sample` を外し、データ取得処理を前段に追加します。
