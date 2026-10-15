# ESI-04のCubism編集元

`rinne-el-rigged-working.cmo3` はESI-03の編集元を別ファイルへコピーし、Cubism Editor 5.3.04 FREEで編集したものです。元のESI-02/03編集元は変更していません。

Body_baseから両腕を独立させ、肩を支点とする回転、眉の上下と傾き、閉じた口の変形を追加しました。新しいメッシュも既存のテクスチャを共有します。元絵を画像生成で描き直す工程はありません。

SDK 4.2向けに `src/esi/resources/rinne-el04` へ書き出します。テクスチャアトラスを自動再配置しないでください。保存後に編集元を閉じて再び開き、別フォルダーへ出力した5ファイルが一致することを確認しています。

動作確認: `python scripts/validate-rinne-esi04.py --model src/esi/resources/rinne-el04/rinne-el.model3.json --output <確認画像の保存先>`。
