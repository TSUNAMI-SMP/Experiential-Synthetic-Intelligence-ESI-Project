# Windows での ESI-01

初めて導入する場合は、[専門用語を減らした導入手順](はじめての導入.md)から進めてください。「プロジェクトのルート」は、`README.md`、`scripts`、`src` が入っている一番外側のフォルダーです。

対象は Windows 11、RTX 5060 8GB、Ryzen 7 5700X、RAM 32GB です。今回の実装・自動テストは Linux CPU 環境で実行したもので、Windows の機器や CUDA の動作を確認済みとはしていません。

## Python とモデル

Python 3.12 を入れ、プロジェクトのルートで PowerShell を開きます。

```powershell
& .\scripts\install-windows.ps1
& .\.venv\Scripts\python.exe .\scripts\download-models.py
```

インストーラーはハッシュ付きの依存ロックを使用します。プラットフォームに合う wheel がない場合は、検証を無効にせず具体的なパッケージの対応版を別の開発作業で確認してください。モデル導入は公式の固定リビジョンから取得し、ASR 約484MB、Qwen Q4_K_M 約2.5GB、凛音エル AIVMX 約253MB の SHA-256 を照合します。

旧版で `colorama>=0.4` の固定版・ハッシュ不足が表示された場合は、GitHub の最新版の `requirements.lock` に差し替えて、同じインストーラーを再実行してください。Windows が必要とする colorama 0.4.6 を公式 PyPI のハッシュ付きで追加しました。Windows / Python 3.12 を対象にした依存関係の解決は確認済みですが、Windows 上の実インストール完了は別の確認です。

## llama.cpp と AivisSpeech

1. [llama.cpp 公式リリース](https://github.com/ggml-org/llama.cpp/releases)から Windows x64 / RTX 5060 に対応する CUDA 版を入れます。クラウド検証版は b11429 です。公式チェックサムを照合し、GPU 対応の NVIDIA ドライバーを使用してください。CPU 版が起動することだけで GPU 動作済みとは判断しません。
2. 別の PowerShell でサーバーを起動します。実機 VRAM が不足する場合は GPU レイヤー数を下げ、条件を記録します。

```powershell
.\llama-server.exe --model C:\path\to\project\models\qwen3-4b\Qwen3-4B-Q4_K_M.gguf --host 127.0.0.1 --port 8080 --ctx-size 4096 --parallel 1 --threads 2 --gpu-layers 99 --reasoning off --jinja
```

3. [AivisSpeech](https://aivis-project.com/speech/) を導入します。AivisSpeech を起動すると同梱エンジンがポート10101で動きます。単独エンジンが必要なら、[公式 Engine リリース](https://github.com/Aivis-Project/AivisSpeech-Engine/releases/tag/1.2.0)か、AivisSpeech に同梱された実行ファイルを使います。CPU 動作で開始します。

```powershell
.\run.exe --host 127.0.0.1 --port 10101 --no-use_gpu
```

モデルのライセンス・配布元の利用条件は、同梱ファイルと公式モデル情報に従ってください。音声の声色を人格設定として扱う設計ではありません。

4. AivisSpeech の「設定」→「音声合成モデルの管理」で、指定された [凛音エル](https://hub.aivis-project.com/aivm-models/f5017410-fbb5-49e1-97cb-e785f42e15f5) を追加します。別の方法として、`download-models.py` が取得した検証済みファイルを ESI の管理コマンドで登録できます。

```powershell
& .\.venv\Scripts\esi.exe --config config\windows.toml install-voice --file models\aivis\f5017410-fbb5-49e1-97cb-e785f42e15f5.aivmx
```

`doctor` が凛音エル v1.0.0、話者 UUID `d2c99ca6-73e5-486c-994e-ee0ce2d74928`、スタイル「ノーマル」を報告することを確認します。数値スタイル ID は対象話者から自動取得します。版・話者・スタイルの不一致は停止して表示します。モデルは ACML 1.0、制作者は kokuren / 電脳天使工業です。

## ESI-01 の観察と音声テスト

```powershell
& .\.venv\Scripts\esi.exe --config config\windows.toml doctor
& .\.venv\Scripts\esi.exe --config config\windows.toml gui
```

停止中に入力マイク・出力スピーカーを選び、「音のテスト」で再生先を確認します。「開始時にマイクを使う」が有効なら「開始」でマイクも開始します。「マイクを使う／止める」で本体と独立に切り替えられます。最初はヘッドホンを使用します。「いまの動作」の入力音量・聞き取り・検討・発声と、「詳しい観察」の状態・候補・結果を確認してください。[画面の見方と更新方法](画面と音声の確認.md)も参照してください。

```powershell
# GUI を使わずに実マイクで継続実行（Ctrl+C で保存）
& .\.venv\Scripts\esi.exe --config config\windows.toml --database data\microphone.sqlite3 run --microphone

# 実モデルの録音処理を含む統合テスト
$env:ESI_RUN_INTEGRATION = "1"
$env:ESI_ASR_MODEL_DIR = "$PWD\models\faster-whisper-small"
& .\.venv\Scripts\python.exe -m pytest -q
```

マイクが16kHz monoに対応しない場合は録音ファイル入力を利用し、デバイス別の取得/変換を調整する必要があります。音声デバイス列挙は `doctor` で確認できます。マイク失敗・TTS 失敗・モデル未起動は技術状態に表示し、自発的な沈黙と区別します。反響対策は混入表示までで、エコーキャンセルは未実装です。

## 実機で残る検証

- GPU ログに CUDA offload が記録され、VRAM が表示処理を含めた予算内に収まること。
- マイク・ヘッドホン・声の割り込み、TTS 混入の実測。
- GUI の開始・停止・保存・分岐・学習切替・録音入力・結果訂正。
- 発話終了から音声までの技術遅延、CPU・RAM・VRAM、長時間実行時のキュー・保存容量。

設計書の3〜8秒は目標です。クラウド CPU の短いテストの時間を、この実機の遅延に読み替えません。
