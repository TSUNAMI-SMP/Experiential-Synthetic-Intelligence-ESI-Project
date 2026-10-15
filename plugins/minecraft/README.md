# ESI Minecraft Bridge 0.4.4

利用者向けの導入と試し方は [Minecraft連携の手順](../../docs/ESI-04-Minecraft連携.md) を参照してください。`release/esi-minecraft-bridge-0.4.4.jar` が配布用MODです。テスト用のMODやMinecraft本体は同梱しません。

## ビルド

JDK 21を用意し、このディレクトリーで実行します。

```powershell
.\gradlew.bat build
.\gradlew.bat runGameTest
```

Linux/macOSでは `./gradlew build`、`./gradlew runGameTest`。Gradle 9.2.1、Loom 1.14.10、Minecraft 1.21.11の公式マッピング、Fabric API 0.141.6+1.21.11、Carpet 1.4.194を固定しています。`build`にも自動ゲームテストを含めています。配布するのは `build/libs/esi-minecraft-bridge-0.4.4.jar` です。

`runGameTest` は専用のテストワールドを起動します。製品にはない試験用の床・道具・食べ物をテストソースでのみ作り、通常の物理移動、採掘時間、道具の摩耗、拾得、食事時間、アイテム消費とゲージ回復、停止、再接続の保存、見えていない対象の拒否を確認します。

実際のPythonクライアントと言語モデルも使う試験では、`ESI_MC_E2E` に空の試験用ディレクトリーを指定して `runGameTest` を起動し、同じ変数とローカルLLMを指定して以下を実行します。

```bash
ESI_MC_E2E=/path/to/private-test-folder pytest -q tests/test_esi04_minecraft_integration.py
```

この試験用フォルダーには一時的な接続コードが作られるため公開しません。テスト用HTTP待機は通常の `runGameTest` では即終了します。

## プラグインと通信

ESIの同梱定義は `src/esi04/resources/plugins/minecraft.json` です。外部のJSON定義は、ESIのセーブと同じフォルダーの `plugins` に追加されます。任意のPythonをインポートする方式ではなく、宣言した知識・操作・身体条件と、別プロセスの接続先を組み合わせる方式です。定義だけを追加しても、新しいゲームの接続プログラムは自動生成されません。

API v1は現在のMinecraft用観察・行動スキーマに対応します。新しい種類の環境を足す場合は、その接続先と観察・身体・行動のアダプターを追加します。Python側のスキーマ・接続処理は `esi04/plugins.py`、環境切り替えは `plugin_session.py`、非同期処理は `plugin_runtime.py`、Minecraftの基本知識と選択可能な行動は `minecraft.py` に分離しています。

MODは `127.0.0.1:8766–8775` にだけ待ち受け、利用者の `/esi link` で発行したBearerコードと、接続ごとの `X-ESI-Lease` を照合します。WebブラウザーのOrigin付き要求を拒否します。任意コマンド・シェルの実行口は設けていません。

| API | 内容 |
| --- | --- |
| POST `/v1/connect` | protocol・plugin_id・character_idを照合して参加し、leaseを発行 |
| GET `/v1/state` | 自分の身体・所持品・見える周囲・直前の操作結果 |
| POST `/v1/action` | 観察IDと操作IDに結び付いた1回の操作 |
| POST `/v1/stop` | 現在の移動・使用・採掘を解除 |
| POST `/v1/disconnect` | 操作を止めて通常の退出・保存 |

観察はブロック半径8・視線遮蔽で制限します。隠れた鉱石、シード、他人の所持品は渡しません。現在の可視リストは最大40ブロック・12エンティティ・12足場、同じブロック種は最大8です。言語モデルへの入力はさらに小さく選びます。決定が遅い場合も、実行前に最新の観察で同じワールド・対象・所持品を確認します。

身体の操作はMinecraftのサーバースレッドに渡します。移動はCarpetの通常入力、近距離の経路は見える足場からのA*、採掘・食事・設置は通常のATTACK/USEです。初参加以外の位置移動にはテレポートを使いません。再接続の位置・ディメンションは保存データから復元します。

Carpetの利用・プレイヤーデータの読み込み方法に関する通知は [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt) を参照してください。
