# 最新の原画に合わせたモデル（2026-10-15）

`rinne-el-rigged-working.cmo3` が今回の保存済み編集元です。通常の顔・まつげ・眉・口・髪・輪は `src/esi/resources/rion-el.png` の画素を使います。`rinne-el-original-head.psd` はそのPNGを格納したPSD、`rinne-el-original-blink.psd` は閉眼の別作画、`rinne-el-original-style-closed-eyes.png` は閉眼のPNGです。両PSDの参照・表示層は独立したPSD読取り器で元PNGとの画素一致を確認しました。

26部品の描画とまばたき前後を確認し、CMO3を開き直した再書き出しで実行用5ファイルがすべて一致しました。既存の5本指の胴体素材を保持します。手足の独立した動きや広い顔向きの遠近作画はありません。ESI-02の比較は [顔比較](../esi02-face-comparison/README.md)、実機は [Windows確認](../../ESI-02-Windows実機確認.md)。

## ESI-03 の髪の輪郭修正

原画の輪郭線を切り取っていた髪の9メッシュをEditorで修正しました。頭頂部の境界は11頂点から40頂点へ増やし、外周の線を含めて滑らかにつなげています。原画PNG・PSD・テクスチャの画素は変更していません。前髪は顔の上、後ろ髪は顔の下、眉は髪の上という描画順を保持します。

原画の髪の線に沿った18地点、15種類の変形、開眼・閉眼・再開眼を実描画で検証。開眼と再開眼は画素一致し、閉眼のまつげ幅は左右85/84ピクセルで、開眼の幅を超えません。保存した編集元を開き直した再出力でも、同梱する5ファイルがすべて一致しました。[ESI-03の比較と測定](../../esi03-avatar-comparison/README.md)を参照してください。

以下は以前の制作資料の記録です。

# 凛音エルの編集元と制作素材

現在の編集元は `rinne-el-rigged-working.cmo3` です。このフォルダーには以前の生成下書きや組み立て途中のファイルも制作履歴として保存しています。実行用モデルは `src/esi/resources/rinne-el/` に置いています。

## 最新の編集元：目・眉・首の追加修正

以下の制作途中の説明は以前の工程の記録です。現在の `rinne-el-rigged-working.cmo3` は12部品を持つ編集元で、実行用モデルを `src/esi/resources/rinne-el/` へ書き出しています。首を含む胴500、顔600、襟の前面650、髪900、独立した眉950の描画順です。前回の顔450では胴に描かれた首が顔にかぶるため、襟の分離も含めて修正しました。

通常の目は117%へ拡大し、左右を反転した同じ形へ登録。開眼・閉眼の素材に含まれていた眉を表示範囲から取り除きました。`brows-separated.png` は原画を参照して画像編集機能で作成した透明の眉素材で、原画と同じ位置・太さの抽出ではありません。Editorで位置、縦の太さ、不透明度を調整し、片側を反転して左右をそろえています。`rinne-el-brows-separated.psd` の各レイヤーは独立したPSD読取り器でこのPNGとの画素一致を確認しました。CMO3には使用する画像の画素が埋め込まれています。

閉眼の形・幅・位置も調整し、実際の描画で開眼を超えない幅と左右の一致を確認。開眼→閉眼→再開眼の比較では最初と最後の画像が画素単位で一致しました。現在のCMO3を開き直した再書き出しも実行用5ファイルと一致しています。画像と測定方法は [修正比較](../esi02-face-comparison/README.md) を参照してください。

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

## リギングの作業保存

`rinne-el-rigged-working.cmo3` はEditorで保存・検証された現在の組み立てデータの別名コピーです。髪と肌を独立したArtMeshへ登録し、古い頭・参照用画像・閉じた口の不要なArtMeshを組み立てから取り除きました。原PNG・PSD・最初の原画保存用CMO3は保持しています。

現在の表示部品は9個。左右の開いた目と閉じた目の不透明度キー、`ParamMouthOpenY` の連続した縦縮小、`ParamBreath` の100〜100.8%の変形、`ParamAngleZ` の首の位置を軸にした±4度、`ParamAngleX/Y` の小さい追従移動、`ParamBodyAngleX` の±0.75度、髪と輪の独立した回転を設定しました。頭のX/Yは小さい移動で、遠近を伴う大きな顔向きの作画を完成したものではありません。

物理演算はEditorで「Head Input」「Hair (Short)」を用い、`ParamHairFront` と `ParamHairSide`（輪）への出力を登録。9部品を2048×2048のテクスチャ1枚へ自動配置し、未配置一覧が空になることを確認しました。出力先SDKは4.2。保存後検証はSUCCESS。MOC3書き出し操作でEditorのイベント処理が応答しなくなり、保存済みプロジェクトから再試行中です。真正なMOC3・ESI統合・Windows実機確認が成功するまでは完成版と扱いません。

## 実行用モデルの書き出し・検証（2026-10-14）

Editor再起動後、SDK4.2向けの真正なMOC3・model3.json・physics3.json・cdi3.json・2048×2048テクスチャを書き出しました。Editorの設定画面で左右の目を自動瞬き、口をリップシンク対象へ登録。呼吸は実際の形状が変わらなかった拡縮指定から、上部ベジェ制御点を動かす変形へ修正し、ネイティブCoreで形状変化を確認しています。最初の100〜100.8%という記述は途中の設定記録で、完成した呼吸の検証結果ではありません。

胴と手足のメッシュを細かくし、輪郭の外側の余白を調整して指先を保持。アトラスを再生成しました。原PNGの画素は変更していません。現在の編集元は `rinne-el-rigged-working.cmo3`。通常の保存と保存後検証SUCCESSを確認済み。

実行用モデルは `src/esi/resources/rinne-el/`。ネイティブCoreで9個のArtMesh、11状態の形状・不透明度・透過描画、出力音量で口の実メッシュが開くこと、中断で閉じること、瞬き・呼吸・物理演算・再読込み・終了を確認しました。新規の表示設定で標準モデルを使い、静止画や独自モデルの既存選択を保持します。Windows/GPU/実音声は未確認で、正式配布の条件はまだ満たしていません。

保存したCMO3を開き直して再書き出し、検証済みの実行用モデルと5ファイルすべてがバイト単位で一致することを確認しました。制作の続きを、保存した編集元から再開できます。

## 手・目・眉・瞬き・首と襟の修正（2026-10-14）

胴の制作途中の原画は両手が4本だったため、画像編集機能で親指＋4本の指へ修正した `body-five-fingers.png` を追加しました。対応する `rinne-el-hands-correction.psd` は独立したPSD読取り器でPNG画素との一致を確認済みです。以前の素材は制作履歴として保持し、現在の標準モデルと区別します。

Editorで胴の画像を差し替え、境界を細かくするメッシュを再生成。開いた目・眉と閉じた目・眉は、それぞれ片側の画像を反転して形と高さを揃え、左右の瞬きへ結び直しました。瞬きキー間で頂点が移動しないことを検証しています。肌・首の描画順は450、胴・襟は500です。

ESI-02の描画側は、このモデルの開閉2枚を開度0.5以下で閉眼、それより上で開眼へ切り替えます。連続した瞼の変形を追加したものではありません。開閉の途中で2枚の虹彩・まぶたが重なる問題を修正しました。

保存した `rinne-el-rigged-working.cmo3` を開き直して再書き出し、同梱するMOC3・model3.json・physics3.json・cdi3.json・texture_00.pngの5ファイルすべてがバイト単位で一致。13状態の描画、開閉両方の左右の形・高さ・顔中心の一致、瞬き途中と閉眼のフレーム一致、口・呼吸・髪・輪の形状変化を検証しました。利用者からWindows起動の成功報告を受けていますが、GPUと実音声の結果は未確認です。
