# 凛音エルの制作途中素材

このフォルダーは部品分離とCubism制作を続けるための保存先です。完成モデルや、位置合わせ済みの分離原画ではありません。実行時のアバターには登録していません。

`rinne-el-source-prototype.psd` は1024×1536のRGBA素材を4レイヤーに格納したPSDです。元の立ち絵だけを表示し、生成した部品は非表示にしてあります。PNGのレイヤー画素は変更せず保存しています。PSDの統合プレビューは形式に合わせて白背景を含むRGBと透明度で符号化するため、半透明の色には8ビット量子化が入ります。

| レイヤー | 元のファイル | 残る作業 |
| --- | --- | --- |
| `Original_reference` | `src/esi/resources/rion-el.png` | 参照用。表示用の原本を維持 |
| `Face_skin_prototype` | `face-skin-prototype.png` | 顔の生成下書き。元絵より大きく、位置と輪郭が異なる。拡縮・位置合わせ、額と耳・首の清書が必要 |
| `Open_eyes_prototype` | `open-eyes-prototype.png` | 目の生成下書き。元絵と寸法・形状が異なる。左右、白目、虹彩、まぶた、ハイライトの分離が必要 |
| `Open_mouth_prototype` | `open-mouth-prototype.png` | 口の生成下書き。元絵との位置と形状の照合、上下唇・口内の分離が必要 |

生成素材は元の立ち絵を画像編集機能に渡して作成しました。キャンバスの大きさは一致しますが、部品の登録位置は一致していません。薄い不要な画素もあるため、そのままArtMeshを自動生成して完成扱いにしないでください。髪・輪・衣服・腕・脚の個別レイヤーはまだこのPSDにありません。以前の部品表 `../rion-el-parts-draft.png` も下書きとして保持しています。

PSDの再作成はリポジトリのルートで次のコマンドを実行します。既存のPSDを上書きしないため、新しい保存先を指定してください。Python 3.12と `Pillow==12.3.0` が必要です。

```bash
python scripts/build-avatar-psd.py \
  --reference src/esi/resources/rion-el.png \
  --layer Face_skin_prototype=docs/avatar-materials/esi02-source-work/face-skin-prototype.png \
  --layer Open_eyes_prototype=docs/avatar-materials/esi02-source-work/open-eyes-prototype.png \
  --layer Open_mouth_prototype=docs/avatar-materials/esi02-source-work/open-mouth-prototype.png \
  --output /tmp/rinne-el-source-next.psd
```

独立したPSD読込みライブラリ `psd-tools==1.24.0` でレイヤー名、表示状態、サイズ、すべてのレイヤー画素、統合プレビューの透明度を検証しました。元PNGのハッシュとPSD・生成素材のハッシュは `source-manifest.json` に保存しています。

Cubism Editor 5.3.04 FREE版でこのPSDを取り込み、`rinne-el-source-prototype.cmo3` を保存しました。Editorの保存後検証は `SUCCESS`。元絵が表示され、追加下書きが非表示になることを目視確認しています。CMO3は未リギングの制作途中プロジェクトで、実行用MOC3ではありません。

追加の頭・胴・輪、左右それぞれの開いた目と閉じた目、閉じた口を画像編集機能で生成し、9部品と元絵を `rinne-el-assembly-prototype.psd` に格納しました。各部品には寸法と位置の差があり、閉じた右目には向きの修正も必要です。組み立てと動きの制作はこのPSDを使って進めます。髪は現在の頭素材に含まれており、独立した髪の清書と物理演算は未完成です。

このクラウドはDebianで、Editorは公式に対応していないWine 10上で動かしています。32ビットのインストーラー／ランチャーはDLLエラーで起動できず、公式配布物の同梱64ビットJavaと `CubismEditor5.bat` を使いました。描画はMesa llvmpipe OpenGL 4.5。Editor本体やCoreの改変、ライセンス判定の変更、Editor本体のESIへの同梱は行っていません。Windows実機での成功とは区別してください。

Cubism制作で必要なパラメータと受け渡し条件は [親フォルダーの制作手順](../README.md) を参照してください。

組み立てCMO3には左右の目の配置・向き修正と、`ParamEyeLOpen` / `ParamEyeROpen` の開閉端での不透明度キーを追加しました。Editor上で両目の開眼と閉眼を確認しています。髪のみ・肌のみの追加素材は `hair-separated-prototype.png` と `face-skin-separated-prototype.png`、格納PSDは `rinne-el-head-separation-prototype.psd`。まだ組み立てに登録しておらず、位置・寸法の調整が必要です。口のメッシュとリギング、連続したまぶたの変形、頭・身体・髪の動き、MOC3は制作中です。
