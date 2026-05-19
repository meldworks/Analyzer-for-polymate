# Polymate Multi-CSV Analyzer

PolymateのCSVを複数同時に読み込み、タスク・条件・試行・チャンネルごとに比較解析するStreamlit製Webアプリです。

- Raw / Filtered波形表示
- 短時間FFT (`scipy.signal.spectrogram`) によるスペクトログラム
- 帯域パワー (Delta / Theta / Alpha / Beta / Gamma) の計算
- 任意メタ項目での2グループ比較・任意ペアでの変化率比較 (Open vs Close / Before vs After などに固定しない)
- チャンネル品質確認
- FFT CSV出力 (`mind_*.csv` 互換形式) と既存FFT CSVの読み込み

> ⚠️ EEG指標から集中・リラックス・疲労・ストレスなどを断定する用途には使えません。あくまで信号解析結果として扱ってください。

---

## インストール

Python 3.10 以降を推奨します。

```bash
pip install -r requirements.txt
```

## 起動方法

```bash
streamlit run app.py
```

ブラウザが開かない場合は表示される URL (例: `http://localhost:8501`) を手動で開いてください。

---

## 使い方の流れ

### 1. CSVをアップロードする (サイドバー)

サイドバーの "Upload CSV files" から **複数CSVを同時にドラッグ&ドロップ** できます。

Polymateの標準形式 (`Sampling Rate(Hz),500` などのメタ行 + `"CLOCK","1","2",...` のヘッダ行を持つCSV) を自動判定して読み込みます。  
列名の前後の空白は自動で `strip` されます。

### 2. ファイル名ルールを設定する (タブ1: File Overview)

ファイル名から `Subject / Task / Trial / Phase` を自動推定します。  
ルールは固定ではなく、**区切り文字** と **各位置に対応するメタ項目** を画面上で変更できます。

例:
- `Miyu-Aroma-1.CSV` → delimiter `-`, 1=Subject, 2=Task, 3=Trial
- `Aroma_Miyu_trial1.CSV` → delimiter `_`, 1=Task, 2=Subject, 3=Trial

`before / after / pre / post` を含む要素は自動でPhase扱いになります (チェックボックスで無効化可)。

ルールは **JSON保存・読み込み可能** です。

### 3. メタデータを手動修正する (タブ1)

タブ1の表で `Subject / Task / Trial / Phase / Memo / Sampling Rate` を編集できます。  
自動推定はあくまで初期値で、解析時はこの編集後の値が正として使われます。

Sampling Rate が CSV から読めなかったときは、この表で手動入力してください。

### 4. チャンネル名と種類を設定する (タブ2: Channel Settings)

CSV内の `"1"`, `"2"`, `"ECG"` などは仮の名前です。  
タブ2で **Display Name** を編集して `Fp1`, `Fp2`, `O1`, `O2` などに変更してください。

| 項目 | 説明 |
|------|------|
| Original Channel Name | CSV列名 (固定。変更不可) |
| Display Name | 画面・出力に使う名前 |
| Channel Type | EEG / ECG / EOG / EMG / Other / Exclude |
| Use for EEG Average | 平均PSD・条件比較に使うか |
| Memo | 電極位置などのメモ |

チャンネル設定は **JSON保存・読み込み可能** で、全ファイルに一括反映するボタンもあります。

### 5. 解析パラメータを設定する (サイドバー)

| 項目 | デフォルト | 説明 |
|------|-----------|------|
| 追加フィルタ | **OFF** | Polymateは標準でフィルター済みのため通常OFF推奨 |
| HPF / LPF | 1Hz / 50Hz | フィルタON時のみ有効 (Butterworth, `filtfilt` でゼロ位相) |
| Notch | OFF / 50Hz / 60Hz | `scipy.signal.iirnotch` |
| Exclude from start | 10秒 | 解析開始から除外 |
| Exclude before end | 5秒 | 解析終了前から除外 |
| FFT Window | 3.0秒 | spectrogram の `nperseg = fs * win_sec` |
| Frequency limit | 60Hz | スペクトル表示・出力の上限 |

### 6. 解析タブを見る

| タブ | 内容 |
|------|------|
| 3. Waveform | Raw / Filtered波形を縦に並べて表示 |
| 4. FFT / Spectrogram | スペクトログラム + 平均PSD + EEG平均PSD |
| 5. Band Power | Delta〜Gamma の帯域パワー、Alpha/Total、Theta/Beta、Alpha Peak Frequency |
| 6. Per-file Summary | アップロードした **全ファイル × チャンネル** のメトリクス (帯域パワー、Alpha/Total、AlphaPeakFreq、peak-to-peak 等) を1表に。2条件比較ではなく N ファイル分の値を表示。File × Channel のピボット表も |
| 7. Multi-file Comparison | アップロードした **全ファイルを個別の棒として並べて比較**。ファイル間で平均は取らない。色分けで Task/Phase/Subject をグルーピング、X 軸を File/Channel 切替、ヒートマップ、任意の基準ファイルとの比 (ratio) も可能 |
| 8. Group Comparison | Pairing key (Subject+Task+Trial 等) で同じ値のファイルを1グループにまとめ、**グループ内の N ファイルを個別の棒として並べる**。2状態 A/B に固定しない。グループ内の基準ファイルとの比 (ratio / change%) も計算可能 |
| 9. Channel Quality | mean / std / peak-to-peak / 100uV超過率 / 低周波ドリフト / 50-60Hzノイズ |
| 10. FFT CSV Export | 時間窓ごとのFFT CSV出力 (`mind_*.csv`互換) |
| 11. Export Report | summary CSV / 設定JSON をまとめた ZIP 出力、既存FFT CSV閲覧 |

---

## 追加フィルター設定

**デフォルトはOFF** です。Polymateで既にフィルタリングされたCSVをそのまま使う前提です。  
ONにすると以下が適用されます:

- ハイパス / ローパス: `scipy.signal.butter` (2次) + `filtfilt` (ゼロ位相)
- ノッチ: `scipy.signal.iirnotch` + `filtfilt`

タブ3で Raw / Filtered を切り替えて適用前後を比較できます。

---

## FFT 計算方法

`scipy.signal.spectrogram` をベースにした **短時間PSD** が基本です。Welch単独ではありません。

| パラメータ | 値 |
|-----------|-----|
| window | `'hamming'` |
| mode | `'psd'` |
| return_onesided | `True` |
| nperseg | `fs * win_sec` (デフォルト3秒) |
| noverlap | `(win_sec - 1) * fs` (= 1秒ステップ) |
| nfft | `nperseg` 以上の次の2のべき乗 |

周波数分解能: `df = fs / nfft`  
例: `fs=500Hz, win_sec=3s` → `n=1500`, `nfft=2048`, `df≈0.244Hz`  
このとき出力列は `F0, F0.24, F0.49, F0.73, ...` のようになります。

スペクトログラムから以下を派生計算します。
- 平均PSD: 時間方向に平均
- 帯域パワー: 周波数方向に対象範囲を平均
- FFT CSV: 各時間窓を1行として出力

---

## FFT CSV出力形式

`mind_*.csv` と同じく、**1行=1時間窓** で出力します (全区間平均1行ではありません)。

列構成:

```
Time, Delta, Theta, Alpha1, Alpha2, Beta1, Beta2, Gamma1, Gamma2, F0, F0.29, F0.59, ...
```

- `Time`: 対応するCLOCK時刻 (取れない場合は秒)
- `Delta`〜`Gamma2`: 各帯域の平均PSD
- `F0, F0.29, ...`: 周波数ごとのPSD (小数第2位までで丸めて末尾0除去 → 例: `0.00→F0`, `0.29→F0.29`, `4.10→F4.1`)

出力ファイル名:
- 個別: `{original_filename_without_ext}_{display_channel_name}_fft.csv`
- 選択チャンネル平均: `{original_filename_without_ext}_selected_channels_mean_fft.csv`
- 条件平均: `{subject}_{task}_mean_fft.csv` または `{subject}_{task}_{phase}_mean_fft.csv`

`metadata.json` には、Subject / Task / Trial / Phase / Original Channel Name / Display Name / Channel Type / Sampling Rate / FFT window / overlap / nfft / Frequency resolution が含まれます。

タブ10で各出力モード (個別 / ZIP一括 / 選択チャンネル平均 / 条件平均) を選んでダウンロードできます。

---

## `mind_*.csv` 形式の読み込み

`mind_*.csv` のように **`Time` 列 + `F0, F0.29...` の周波数列** を含むCSVをアップロードすると、自動で「FFT CSV」として判定されます。

タブ11 "Export Report" の下部 "Existing FFT CSV viewer" で読み込んだFFT CSVを選択すると:

- スペクトログラム表示
- 平均PSD表示
- 帯域パワー時系列 (タブ5でも参照可能)

が表示されます。Raw CSV から再計算は不要です。

---

## 出力ファイルの説明 (Export Report)

タブ11 "Build report ZIP" で以下を含む ZIP が生成されます。

| ファイル | 内容 |
|---------|------|
| `summary_by_file.csv` | 各ファイルのメタ情報一覧 |
| `summary_by_condition.csv` | 帯域パワーのファイル/チャンネル別表 |
| `condition_comparison.csv` | Per-file Summary の全ファイル×チャンネル一覧 |
| `group_comparison.csv` | 2-Group Comparison の結果表 |
| `paired_comparison.csv` | Paired Comparison の結果表 |
| `channel_quality.csv` | チャンネル品質指標 |
| `channel_settings.json` | 全ファイルのチャンネル設定 |
| `filename_rule_settings.json` | ファイル名ルール |
| `analysis_settings.json` | 解析パラメータ (Exclude / FFT / フィルタ等) |

FFT CSV (タブ10) と Report ZIP (タブ11) はそれぞれのタブからダウンロードしてください。

---

## 注意事項

- ECG / EOG / EMG / Other / Exclude のチャンネルは EEG 平均から自動除外します。
- ECG列など、列名から推測可能なものは初期値で `Channel Type=ECG, Use for EEG Average=OFF` にしますが、必ずタブ2で確認してください。
- CSV列名に空白が含まれる場合も `strip` して扱います。
- 欠損値 (NaN) は線形補間して解析し、Overviewに警告を出します。
- Sampling Rate が異なるファイルが混在する場合は警告を出しますが、解析自体は各ファイルの fs を使って進めます。
