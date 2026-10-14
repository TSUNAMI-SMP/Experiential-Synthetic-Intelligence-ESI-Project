# Cubism制作時の画面操作補助

Wine上のEditorでモーダル画面へのキーボード入力が反映されないために用意した開発補助です。ESIの起動処理や配布依存には追加していません。

`UiAgent.java` はJVM標準のJava agentとして、公開されたAWT/Swingの画面部品を列挙し、ボタン、入力欄、選択欄を操作します。Cubismの内部モデルAPI、ファイル形式、Core、ライセンス判定にはアクセスしません。Editorから通常どおり保存・書き出しを行います。Editor本体やベンダー署名付きJARを変更するコードはありません。

JDK 17で次のようにコンパイルし、Manifestの `Premain-Class` を `org.esi.taskui.UiAgent` にしたJARを作ります。独自パッケージを使う必要があります。既定パッケージで読み込むと、Editorの署名付きクラスとの名前空間衝突で起動に失敗します。

```bash
javac --release 17 -d out UiAgent.java
jar --create --file ui-agent.jar --manifest MANIFEST.MF -C out .
```

```text
Manifest-Version: 1.0
Premain-Class: org.esi.taskui.UiAgent

```

Editorの同梱BATを使う場合、JVMの追加起動オプションで `-javaagent:<ui-agent.jarの絶対パス>` と `--add-modules=jdk.httpserver` を指定します。クラウド検証では、Windows形式のパスを `_JAVA_OPTIONS` に設定して起動しました。Java agentの起動、画面部品の読取り、FREEボタンと確認ボタンの操作、拡縮ダイアログへの値設定・確定を確認しています。Windows実機での操作確認は未実施です。

HTTPは `127.0.0.1:8769` にのみ待ち受けます。`GET /tree` が画面部品を返し、`POST /action` は画面操作をSwingのイベント処理へ渡します。操作の受理はモデルの変更成功を意味しないため、入力値と画面・保存結果を確認してください。

Python側は標準ライブラリだけを使います。Editorを起動してから、現在の部品IDを調べて指定します。IDはEditorの再起動時に変わります。

```bash
python ui_control.py find 'Specifying Magnification'
python ui_control.py click 217
python ui_control.py find 'JTextField'
python ui_control.py text 1826 75
```

上のIDは検証時の例です。現在の画面を調べずに再利用しないでください。画面データは既定で実行ディレクトリの `cubism-ui-state/latest-ui-tree.json` に保存します。保存先は `ESI_CUBISM_UI_STATE` で指定できます。確認し終えたらEditorを終了すると補助HTTPサービスも終了します。
