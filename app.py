"""
Polymate Multi-CSV EEG Analyzer (Streamlit)

複数のPolymate CSVを読み込み、タスク/条件/試行/チャンネルごとに比較解析するWebアプリ。
- Raw / Filtered波形
- 短時間FFT (scipy.signal.spectrogram)
- 平均PSD / 帯域パワー / 条件比較
- Open vs Close / Before vs After
- チャンネル品質チェック
- FFT CSV 出力 (mind_*.csv 互換)
- 既存FFT CSV (mind_*.csv) の読み込み
"""

from __future__ import annotations

import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from scipy import signal


# =====================================================================
# 定数 / 既定値
# =====================================================================

CHANNEL_TYPES = ["EEG", "ECG", "EOG", "EMG", "Other", "Exclude"]

# チャンネル名プリセット: (display_name, channel_type) のタプル列を順番通りに当てる
CHANNEL_PRESETS: dict[str, list[tuple[str, str]]] = {
    "Polymate 8ch (T7/T8/Oz/Fpz/Cz/C3/L-Ear/Heart)": [
        ("T7", "EEG"), ("T8", "EEG"), ("Oz", "EEG"), ("Fpz", "EEG"),
        ("Cz", "EEG"), ("C3", "EEG"), ("L-Ear", "EEG"), ("Heart", "ECG"),
    ],
    "国際10-20 7ch + ECG (Fp1/Fp2/T3/T4/O1/O2/Cz + ECG)": [
        ("Fp1", "EEG"), ("Fp2", "EEG"), ("T3", "EEG"), ("T4", "EEG"),
        ("O1", "EEG"), ("O2", "EEG"), ("Cz", "EEG"), ("ECG", "ECG"),
    ],
    "前頭中心 7ch + ECG (Fp1/Fp2/F3/F4/C3/C4/Cz + ECG)": [
        ("Fp1", "EEG"), ("Fp2", "EEG"), ("F3", "EEG"), ("F4", "EEG"),
        ("C3", "EEG"), ("C4", "EEG"), ("Cz", "EEG"), ("ECG", "ECG"),
    ],
}

BAND_DEFS: dict[str, tuple[float, float]] = {
    "Delta": (1.0, 4.0),
    "Theta": (4.0, 8.0),
    "Alpha1": (8.0, 10.0),
    "Alpha2": (10.0, 12.0),
    "Alpha": (8.0, 12.0),
    "Beta1": (12.0, 20.0),
    "Beta2": (20.0, 30.0),
    "Beta": (12.0, 30.0),
    "Gamma1": (30.0, 40.0),
    "Gamma2": (40.0, 50.0),
    "Gamma": (30.0, 50.0),
    "Total": (1.0, 50.0),
}

# FFT CSV 出力に含める帯域 (mind_*.csv の列順に揃える)
EXPORT_BANDS = ["Delta", "Theta", "Alpha1", "Alpha2", "Beta1", "Beta2", "Gamma1", "Gamma2"]


# =====================================================================
# データクラス
# =====================================================================

@dataclass
class ChannelSetting:
    """1チャンネル分の設定。元列名は固定、表示名や種類はユーザーが変更可能。"""
    original_name: str
    display_name: str
    channel_type: str = "EEG"
    use_for_eeg_average: bool = True
    memo: str = ""


@dataclass
class FileData:
    """1ファイル分の読み込み済みデータ。"""
    file_name: str
    subject: str = ""
    task: str = "Unknown"
    trial: str = ""
    phase: str = "none"
    memo: str = ""
    sampling_rate: Optional[float] = None
    unit: str = ""
    duration: float = 0.0
    raw_dataframe: Optional[pd.DataFrame] = None  # CLOCK列を含むDataFrame
    time_array: Optional[np.ndarray] = None  # 秒単位
    clock_array: Optional[np.ndarray] = None  # 文字列のCLOCK列
    channel_list: list[str] = field(default_factory=list)
    channel_settings: list[ChannelSetting] = field(default_factory=list)
    status: str = "OK"
    error: str = ""
    is_fft_csv: bool = False  # mind_*.csv のような既存FFT CSV か
    fft_csv_dataframe: Optional[pd.DataFrame] = None  # 既存FFT CSVの中身
    order: int = 0  # グラフ表示順 (1始まりが推奨)。0 はまだ番号未割当


# =====================================================================
# ファイル名 → メタ推定
# =====================================================================

DEFAULT_RULE = {
    "delimiter": "-",
    "subject_index": 1,  # 1始まり (0 = 使わない)
    "task_index": 2,
    "trial_index": 3,
    "phase_auto": True,  # before/after/pre/post を Phase として自動検出
}


def split_basename(name: str, delimiter: str) -> list[str]:
    """拡張子を外した上で区切る。delimiter='space' などの特殊指定を解釈する。"""
    base = re.sub(r"\.[^.]+$", "", name)
    if delimiter == "space":
        return base.split(" ")
    return base.split(delimiter)


def estimate_metadata(file_name: str, rule: dict) -> dict:
    """ファイル名ルールに従って Subject / Task / Trial / Phase を推定。

    どれかが取れない場合は空文字 / Unknown / none を入れる。
    """
    parts = split_basename(file_name, rule.get("delimiter", "-"))
    parts = [p.strip() for p in parts if p.strip() != ""]

    phase = "none"
    if rule.get("phase_auto", True):
        for p in parts:
            lower = p.lower()
            if lower in ("before", "after", "pre", "post"):
                phase = lower
                break

    def pick(idx_1based: int) -> str:
        if idx_1based <= 0:
            return ""
        idx = idx_1based - 1
        if idx < len(parts):
            val = parts[idx]
            # phaseとして使った要素はSubject/Task/Trialには使わない
            if rule.get("phase_auto", True) and val.lower() in ("before", "after", "pre", "post"):
                return ""
            return val
        return ""

    subject = pick(int(rule.get("subject_index", 1)))
    task = pick(int(rule.get("task_index", 2))) or "Unknown"
    trial = pick(int(rule.get("trial_index", 3)))

    return {
        "subject": subject,
        "task": task,
        "trial": trial,
        "phase": phase,
    }


# =====================================================================
# CSV パース
# =====================================================================

def _decode_bytes(raw: bytes) -> str:
    """UTF-8 → CP932 → Shift_JIS の順でデコードを試す。"""
    for enc in ("utf-8", "utf-8-sig", "cp932", "shift_jis", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def is_fft_csv_text(text: str) -> bool:
    """先頭行を見て、F0/F0.29 のような周波数列を持つ FFT CSV か判定。"""
    head = text.splitlines()[0] if text else ""
    cols = [c.strip() for c in head.split(",")]
    has_freq_col = any(re.fullmatch(r"F\d+(\.\d+)?", c) for c in cols)
    return has_freq_col and "Time" in cols


def parse_polymate_csv(file_name: str, raw: bytes) -> FileData:
    """Polymate CSV を解析して FileData を返す。エラー時は status='Error' を設定。

    形式例:
        Sampling Rate(Hz),500
        Type, 1/1, 1/1, ...
        Unit,uV,uV,...
        "CLOCK"," 1"," 2",...
        17:04:54.000,-6.96,...
    """
    fd = FileData(file_name=file_name)
    try:
        text = _decode_bytes(raw)

        # mind_*.csv のような既存FFT CSVは別ルートで処理
        if is_fft_csv_text(text):
            return parse_fft_csv(file_name, text)

        lines = text.splitlines()
        sampling_rate: Optional[float] = None
        unit = ""
        header_row_idx: Optional[int] = None
        channel_names: list[str] = []

        for i, line in enumerate(lines[:40]):  # 先頭40行をメタ情報候補として走査
            parts = [p.strip().strip('"') for p in line.split(",")]
            if not parts:
                continue
            key = parts[0].lower().replace(" ", "")
            if key.startswith("samplingrate"):
                try:
                    sampling_rate = float(parts[1])
                except Exception:
                    pass
            elif key == "unit":
                # Unitは複数列だが、ひとまず先頭の値を採用
                non_empty = [p for p in parts[1:] if p]
                if non_empty:
                    unit = non_empty[0]
            elif key == "clock" or parts[0].strip().strip('"').upper() == "CLOCK":
                header_row_idx = i
                channel_names = [p for p in parts]
                break

        if header_row_idx is None:
            # CLOCK 列のない CSV: 1行目をヘッダとして読む (fallback)
            df = pd.read_csv(io.StringIO(text))
            df.columns = [str(c).strip() for c in df.columns]
        else:
            df = pd.read_csv(io.StringIO(text), skiprows=header_row_idx, header=0)
            df.columns = [str(c).strip().strip('"') for c in df.columns]

        if df.empty:
            fd.status = "Error"
            fd.error = "データ行がありません"
            return fd

        # チャンネル名 (CLOCK 以外の全列)
        cols = list(df.columns)
        clock_col = None
        for c in cols:
            if c.upper() == "CLOCK":
                clock_col = c
                break

        ch_cols = [c for c in cols if c != clock_col]

        # 数値列のみを残す (数値化を試みて NaN ばかりの列は除外)
        numeric_chs: list[str] = []
        for c in ch_cols:
            converted = pd.to_numeric(df[c], errors="coerce")
            if converted.notna().sum() > 0:
                df[c] = converted
                numeric_chs.append(c)

        if not numeric_chs:
            fd.status = "Error"
            fd.error = "数値チャンネル列が見つかりません"
            return fd

        fd.sampling_rate = sampling_rate  # None の可能性あり (UIで手動入力)
        fd.unit = unit
        fd.raw_dataframe = df
        fd.channel_list = numeric_chs
        fd.clock_array = df[clock_col].astype(str).values if clock_col else None

        if sampling_rate and sampling_rate > 0:
            n = len(df)
            fd.time_array = np.arange(n) / sampling_rate
            fd.duration = n / sampling_rate
        else:
            fd.time_array = np.arange(len(df), dtype=float)
            fd.duration = 0.0

        # チャンネル設定の初期値: ECG/EOG/EMG らしい列名は自動判定
        settings = []
        for ch in numeric_chs:
            ch_type = "EEG"
            use_avg = True
            upper = ch.upper()
            if "ECG" in upper or "EKG" in upper:
                ch_type, use_avg = "ECG", False
            elif "EOG" in upper:
                ch_type, use_avg = "EOG", False
            elif "EMG" in upper:
                ch_type, use_avg = "EMG", False
            settings.append(ChannelSetting(
                original_name=ch, display_name=ch,
                channel_type=ch_type, use_for_eeg_average=use_avg,
            ))
        fd.channel_settings = settings

        # 欠損値の警告
        nan_count = df[numeric_chs].isna().sum().sum()
        if nan_count > 0:
            fd.status = f"OK (NaN={int(nan_count)})"

        return fd
    except Exception as e:
        fd.status = "Error"
        fd.error = f"{type(e).__name__}: {e}"
        return fd


def parse_fft_csv(file_name: str, text: str) -> FileData:
    """mind_*.csv のような既存FFT CSV を読み込む。"""
    fd = FileData(file_name=file_name, is_fft_csv=True)
    try:
        df = pd.read_csv(io.StringIO(text))
        df.columns = [str(c).strip() for c in df.columns]
        fd.fft_csv_dataframe = df
        fd.duration = float(len(df))  # 1行=1秒前後とみなす (簡易)
        fd.status = "FFT CSV"
        fd.channel_list = ["(FFT CSV)"]
        fd.channel_settings = [ChannelSetting(
            original_name="(FFT CSV)", display_name="(FFT CSV)",
            channel_type="Other", use_for_eeg_average=False,
        )]
        return fd
    except Exception as e:
        fd.status = "Error"
        fd.error = f"FFT CSV parse error: {e}"
        return fd


# =====================================================================
# 信号処理
# =====================================================================

def reject_large_peaks(
    sig_array: np.ndarray, fs: float, threshold_uv: float = 75.0,
    margin_ms: float = 100.0,
) -> tuple[np.ndarray, float]:
    """振幅が threshold_uv を超えるサンプル ± margin_ms を NaN にして線形補間する。

    返り値: (補間後の信号, 除去率 0.0-1.0)
    """
    out = sig_array.astype(float).copy()
    n = len(out)
    if n == 0 or fs <= 0:
        return out, 0.0
    # 1) 閾値超え検出
    over = np.abs(out) > threshold_uv
    if not over.any():
        return out, 0.0
    # 2) 検出点の前後 margin_ms までを除外マスクに広げる
    margin = max(int(fs * margin_ms / 1000.0), 1)
    mask = np.zeros(n, dtype=bool)
    idx = np.where(over)[0]
    for i in idx:
        lo = max(i - margin, 0)
        hi = min(i + margin + 1, n)
        mask[lo:hi] = True
    # 3) NaN に置き換えて線形補間
    out[mask] = np.nan
    s = pd.Series(out).interpolate(limit_direction="both").to_numpy()
    # 端が NaN のままなら 0 埋め
    s = np.nan_to_num(s, nan=0.0)
    return s, float(mask.sum()) / n


def apply_additional_filters(
    sig_array: np.ndarray, fs: float,
    hpf: Optional[float], lpf: Optional[float], notch: Optional[float],
    order: int = 2, qval: float = 30.0,
) -> np.ndarray:
    """追加フィルタを適用 (ON時のみ呼び出す前提)。zero-phase で filtfilt。"""
    out = sig_array.astype(float).copy()
    nyq = fs / 2.0
    if hpf and hpf > 0:
        b, a = signal.butter(order, hpf / nyq, btype="high")
        out = signal.filtfilt(b, a, out)
    if lpf and lpf > 0 and lpf < nyq:
        b, a = signal.butter(order, lpf / nyq, btype="low")
        out = signal.filtfilt(b, a, out)
    if notch and notch > 0 and notch < nyq:
        b, a = signal.iirnotch(notch, qval, fs=fs)
        out = signal.filtfilt(b, a, out)
    return out


def next_pow2(n: int) -> int:
    return int(2 ** math.ceil(math.log2(max(n, 1))))


def compute_spectrogram(
    sig_array: np.ndarray, fs: float, win_sec: float = 3.0, fq_lim: float = 60.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """scipy.signal.spectrogram で短時間PSD を計算。

    Returns
        spctgram: (n_freq, n_time) PSD配列
        freq: 周波数配列 [Hz]
        t: 各窓の中心時刻 [s]
    """
    n = max(int(fs * win_sec), 8)
    if n > len(sig_array):
        n = len(sig_array)
    nfft = next_pow2(n)
    noverlap = max(int((win_sec - 1.0) * fs), 0)
    if noverlap >= n:
        noverlap = n - 1
    freq, t, sx = signal.spectrogram(
        sig_array, fs=fs, nperseg=n, nfft=nfft, noverlap=noverlap,
        window="hamming", mode="psd", return_onesided=True,
    )
    # fq_lim でカット
    df = fs / nfft
    cutoff = math.ceil(fq_lim / df) if df > 0 else len(freq)
    cutoff = min(cutoff, len(freq))
    return np.abs(sx[:cutoff, :]), freq[:cutoff], t


def band_power_from_spec(spctgram: np.ndarray, freq: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """[lo, hi] Hz の周波数を平均して帯域パワーの時系列を返す。"""
    mask = (freq >= lo) & (freq < hi)
    if mask.sum() == 0:
        return np.zeros(spctgram.shape[1])
    return np.abs(spctgram[mask, :]).mean(axis=0)


def alpha_peak_frequency(mean_psd: np.ndarray, freq: np.ndarray, lo: float = 7.0, hi: float = 13.0) -> float:
    """Alpha 帯域内のピーク周波数。見つからなければ NaN。"""
    mask = (freq >= lo) & (freq <= hi)
    if mask.sum() == 0:
        return float("nan")
    sub = mean_psd[mask]
    sub_freq = freq[mask]
    if len(sub) == 0 or np.all(np.isnan(sub)):
        return float("nan")
    return float(sub_freq[int(np.nanargmax(sub))])


def freq_label(value: float) -> str:
    """周波数値 → 'F0.29' のような列名文字列。小数第2位まで丸めて末尾0を除去。"""
    rounded = round(float(value), 2)
    s = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return f"F{s}"


def safe_filename(name: str) -> str:
    """ファイル名に使えない文字をアンダースコアに置換。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(name)).strip("_")


def display_file_name(name: str) -> str:
    """グラフ表示用に拡張子を取り除いたファイル名を返す (例: 'Miyu-Close-1.CSV' → 'Miyu-Close-1')。"""
    return re.sub(r"\.[^.]+$", "", str(name))


def get_file_order_labels(state: dict) -> list[str]:
    """File Overview で指定されたユーザー順 (fd.order 昇順) に並べた FileLabel のリスト。

    全グラフの category_orders={"FileLabel": ...} に渡して、表示順を統一する。
    """
    items = []
    for fd in state["files"].values():
        if fd.is_fft_csv or fd.status == "Error":
            continue
        order_val = getattr(fd, "order", 0)
        items.append((order_val if order_val > 0 else 999999, display_file_name(fd.file_name)))
    # order が同じ場合は名前順で安定ソート
    items.sort(key=lambda x: (x[0], x[1]))
    return [label for _, label in items]


def apply_file_order(df: pd.DataFrame, order_list: list[str], file_col: str = "FileLabel") -> pd.DataFrame:
    """DataFrame の指定列をユーザー順に並べ替えた新しい DataFrame を返す。

    plotly の category_orders だけだと barmode や color の組合せで無視されるケースが
    あるため、Categorical 化 + sort_values で確実に並び順を固定する。
    """
    if df.empty or file_col not in df.columns or not order_list:
        return df
    # order_list に無いラベル (例: ユーザーが順序を割り当てていない場合) は末尾に置く
    extra = [v for v in df[file_col].astype(str).unique() if v not in order_list]
    cats = list(order_list) + extra
    df = df.copy()
    df[file_col] = pd.Categorical(df[file_col].astype(str), categories=cats, ordered=True)
    df = df.sort_values(by=[file_col])
    return df



def compute_facet_layout(n_facets: int, plot_px: int = 220, gap_px: int = 80,
                          margin_top: int = 80, margin_bot: int = 120, height_cap: int = 2400) -> tuple[Optional[float], int]:
    """facet_row_spacing と layout.height をピクセルベースで計算する。

    - plot_px: 1ファセット分のプロット領域 (Y方向)
    - gap_px:  ファセット間の余白 (X軸ラベルや facet タイトル分)
    - 戻り値: (facet_row_spacing fraction, total height px)
    """
    if n_facets <= 1:
        total = max(plot_px + margin_top + margin_bot, 420)
        return None, min(total, height_cap)
    total_h = plot_px * n_facets + gap_px * (n_facets - 1) + margin_top + margin_bot
    total_h = min(total_h, height_cap)
    plot_area_h = max(total_h - margin_top - margin_bot, 1)
    spacing = gap_px / plot_area_h
    # plotly の上限は 1/(rows-1) なので、余裕を持って 0.95/(n-1) で頭打ち
    spacing = min(spacing, 0.95 / (n_facets - 1))
    return spacing, total_h



# =====================================================================
# 解析対象区間抽出
# =====================================================================

def slice_signal(sig_array: np.ndarray, fs: float, exclude_start: float, exclude_end: float) -> tuple[np.ndarray, float, float]:
    """開始 / 終了から指定秒数を除外。t_start_sec, t_end_sec も返す。"""
    n = len(sig_array)
    if fs <= 0:
        return sig_array, 0.0, float(n)
    i0 = max(int(exclude_start * fs), 0)
    i1 = max(n - int(exclude_end * fs), i0 + 1)
    i1 = min(i1, n)
    return sig_array[i0:i1], i0 / fs, i1 / fs


# =====================================================================
# Session State 補助
# =====================================================================

def get_state() -> dict:
    """Streamlit session_state へのアクセス補助。"""
    if "files" not in st.session_state:
        st.session_state["files"] = {}  # file_name -> FileData
    if "filename_rule" not in st.session_state:
        st.session_state["filename_rule"] = dict(DEFAULT_RULE)
    # 旧バージョンの FileData をマイグレート (新しく追加した属性のデフォルトを埋める)
    for i, fd in enumerate(st.session_state["files"].values(), start=1):
        if not hasattr(fd, "order"):
            try:
                setattr(fd, "order", i)
            except Exception:
                pass
    return st.session_state


def serialize_channel_settings(fd: FileData) -> list[dict]:
    return [asdict(cs) for cs in fd.channel_settings]


def deserialize_channel_settings(data: list[dict]) -> list[ChannelSetting]:
    out = []
    for d in data:
        out.append(ChannelSetting(
            original_name=str(d.get("original_name", "")),
            display_name=str(d.get("display_name", "")),
            channel_type=str(d.get("channel_type", "EEG")),
            use_for_eeg_average=bool(d.get("use_for_eeg_average", True)),
            memo=str(d.get("memo", "")),
        ))
    return out


# =====================================================================
# 解析ヘルパー
# =====================================================================

def get_processed_signal(
    fd: FileData, ch_name: str, filt_on: bool, hpf, lpf, notch,
    exclude_start: float, exclude_end: float,
    peak_reject: bool = False, peak_threshold: float = 75.0, peak_margin_ms: float = 100.0,
) -> tuple[np.ndarray, np.ndarray]:
    """指定ファイル・チャンネルの解析対象信号と時間軸を返す。

    パイプライン:
        欠損補間 → 追加フィルタ (任意) → 大ピーク除去 (任意) → 前後カット
    """
    if fd.raw_dataframe is None or fd.sampling_rate is None:
        return np.array([]), np.array([])
    if ch_name not in fd.raw_dataframe.columns:
        return np.array([]), np.array([])
    sig_arr = fd.raw_dataframe[ch_name].to_numpy(dtype=float)
    # 1) 欠損は線形補間
    if np.isnan(sig_arr).any():
        sig_arr = pd.Series(sig_arr).interpolate(limit_direction="both").to_numpy()
    # 2) 追加フィルタ (任意)
    if filt_on:
        sig_arr = apply_additional_filters(sig_arr, fd.sampling_rate, hpf, lpf, notch)
    # 3) 大ピーク除去 (任意)。EEG 系チャンネル以外には適用しない方が安全なので、ここでは適用するが
    #    閾値とマージンは UI から制御できる
    if peak_reject:
        sig_arr, _ = reject_large_peaks(sig_arr, fd.sampling_rate, peak_threshold, peak_margin_ms)
    # 4) 前後カット
    sliced, t0, t1 = slice_signal(sig_arr, fd.sampling_rate, exclude_start, exclude_end)
    t_axis = np.arange(len(sliced)) / fd.sampling_rate + t0
    return sliced, t_axis


def compute_band_power_table(spctgram: np.ndarray, freq: np.ndarray, t: np.ndarray) -> pd.DataFrame:
    """帯域パワー時系列の DataFrame を返す (列: Time, Delta, Theta, ...)."""
    out = {"Time": t}
    for band, (lo, hi) in BAND_DEFS.items():
        out[band] = band_power_from_spec(spctgram, freq, lo, hi)
    df = pd.DataFrame(out)
    # 派生指標
    total = df["Total"].replace(0, np.nan)
    alpha = df["Alpha"].replace(0, np.nan)
    beta = df["Beta"].replace(0, np.nan)
    df["Alpha/Total"] = df["Alpha"] / total
    df["Theta/Beta"] = df["Theta"] / beta
    # Beta/Alpha: 脳の活性指標 (大きいほど活性、小さいほどリラックス傾向の目安)
    df["Beta/Alpha"] = df["Beta"] / alpha
    return df


def build_fft_export_dataframe(spctgram: np.ndarray, freq: np.ndarray, t: np.ndarray, clock_array: Optional[np.ndarray], fs: float, exclude_start: float) -> pd.DataFrame:
    """mind_*.csv 互換の FFT CSV (時間窓ごと) を組み立てる。"""
    # 周波数列名
    freq_cols = [freq_label(f) for f in freq]
    # 重複した列名が発生しないようにユニーク化
    seen: dict[str, int] = {}
    uniq_cols = []
    for c in freq_cols:
        if c in seen:
            seen[c] += 1
            uniq_cols.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            uniq_cols.append(c)
    freq_cols = uniq_cols

    # 帯域パワー (mind_*.csv 列順)
    band_cols = {}
    for band in EXPORT_BANDS:
        lo, hi = BAND_DEFS[band]
        band_cols[band] = band_power_from_spec(spctgram, freq, lo, hi)

    # Time列: CLOCK が使える場合は対応する時刻文字列を入れる
    if clock_array is not None and fs > 0:
        time_col = []
        for ti in t:
            idx = int((ti + exclude_start) * fs)
            if 0 <= idx < len(clock_array):
                time_col.append(str(clock_array[idx]))
            else:
                time_col.append(f"{ti:.3f}")
    else:
        time_col = [f"{ti:.3f}" for ti in t]

    data = {"Time": time_col}
    data.update(band_cols)
    for i, c in enumerate(freq_cols):
        data[c] = spctgram[i, :]
    return pd.DataFrame(data)


# =====================================================================
# UI: Sidebar
# =====================================================================

def render_sidebar(state: dict) -> dict:
    """サイドバーを描画して、解析パラメータを dict で返す。"""
    st.sidebar.header("Files & Analysis Settings")

    # file_uploader はリセット時にキーを変えることで内部状態をクリアできる
    if "upload_counter" not in state:
        state["upload_counter"] = 0
    uploaded = st.sidebar.file_uploader(
        "Upload CSV files (multi-select)", type=["csv", "CSV"], accept_multiple_files=True,
        key=f"file_uploader_{state['upload_counter']}",
    )
    if uploaded:
        # 既存の最大 order を取得し、新規ファイルには続き番号を割り当て
        max_order = max((getattr(fd, "order", 0) for fd in state["files"].values()), default=0)
        for uf in uploaded:
            if uf.name in state["files"]:
                continue
            fd = parse_polymate_csv(uf.name, uf.getvalue())
            # ファイル名ルールに従ってメタ推定
            est = estimate_metadata(uf.name, state["filename_rule"])
            fd.subject = est["subject"]
            fd.task = est["task"]
            fd.trial = est["trial"]
            fd.phase = est["phase"]
            max_order += 1
            fd.order = max_order
            state["files"][uf.name] = fd
        st.sidebar.success(f"{len(uploaded)} ファイルを処理しました")

    files: dict[str, FileData] = state["files"]
    valid_files = {k: v for k, v in files.items() if v.status != "Error" and not v.is_fft_csv}

    # 表示フィルタ
    subjects = sorted({fd.subject for fd in valid_files.values() if fd.subject})
    tasks = sorted({fd.task for fd in valid_files.values() if fd.task})
    trials = sorted({fd.trial for fd in valid_files.values() if fd.trial})
    phases = sorted({fd.phase for fd in valid_files.values() if fd.phase})

    st.sidebar.markdown("---")
    st.sidebar.subheader("Display Filters")
    sel_subjects = st.sidebar.multiselect("Subject", subjects, default=subjects, key="sb_subjects")
    sel_tasks = st.sidebar.multiselect("Task", tasks, default=tasks, key="sb_tasks")
    sel_trials = st.sidebar.multiselect("Trial", trials, default=trials, key="sb_trials")
    sel_phases = st.sidebar.multiselect("Phase", phases, default=phases, key="sb_phases")

    # チャンネル選択
    all_channels = sorted({cs.display_name for fd in valid_files.values() for cs in fd.channel_settings})
    st.sidebar.markdown("---")
    st.sidebar.subheader("Channel Selection")
    sel_channels = st.sidebar.multiselect("Use channels", all_channels, default=all_channels, key="sb_channels")
    excl_channels = st.sidebar.multiselect("Exclude channels", all_channels, default=[], key="sb_excl_channels")

    # 追加フィルタ
    st.sidebar.markdown("---")
    st.sidebar.subheader("Additional Filters")
    st.sidebar.caption("Polymateデータは標準でフィルター済みのため、通常はOFF推奨")
    filt_on = st.sidebar.checkbox("Enable additional filter", value=False)
    hpf = st.sidebar.number_input("HPF [Hz]", min_value=0.0, max_value=200.0, value=1.0, step=0.5, disabled=not filt_on)
    lpf = st.sidebar.number_input("LPF [Hz]", min_value=0.0, max_value=500.0, value=50.0, step=0.5, disabled=not filt_on)
    notch_choice = st.sidebar.selectbox("Notch", ["OFF", "50Hz", "60Hz"], index=0, disabled=not filt_on)
    notch = 0.0 if notch_choice == "OFF" else float(notch_choice.replace("Hz", ""))

    # 解析対象区間
    st.sidebar.markdown("---")
    st.sidebar.subheader("Analysis window")
    st.sidebar.caption("データの取り始め・取り終わりはノイズが乗りやすいので前後 5 秒を除外するのが推奨")
    exclude_start = st.sidebar.number_input("Exclude from start [s]", 0.0, 600.0, 5.0, 1.0)
    exclude_end = st.sidebar.number_input("Exclude before end [s]", 0.0, 600.0, 5.0, 1.0)

    # アーティファクト除去
    st.sidebar.markdown("---")
    st.sidebar.subheader("Artifact rejection")
    st.sidebar.caption("瞬目や体動などで生じる大きなピークを除去 (該当サンプル ± マージンを線形補間)。")
    peak_reject = st.sidebar.checkbox("Reject large peaks", value=True)
    peak_threshold = st.sidebar.number_input(
        "Threshold |amplitude| [uV]", 10.0, 1000.0, 75.0, 5.0, disabled=not peak_reject,
    )
    peak_margin_ms = st.sidebar.number_input(
        "Margin around peak [ms]", 0.0, 1000.0, 100.0, 10.0, disabled=not peak_reject,
    )

    # FFT 設定
    st.sidebar.markdown("---")
    st.sidebar.subheader("FFT settings")
    win_sec = st.sidebar.number_input("Window [s]", 0.5, 30.0, 3.0, 0.5)
    fq_lim = st.sidebar.number_input("Frequency limit [Hz]", 5.0, 250.0, 60.0, 5.0)

    return {
        "sel_subjects": sel_subjects, "sel_tasks": sel_tasks,
        "sel_trials": sel_trials, "sel_phases": sel_phases,
        "sel_channels": sel_channels, "excl_channels": excl_channels,
        "filt_on": filt_on, "hpf": hpf, "lpf": lpf, "notch": notch,
        "exclude_start": exclude_start, "exclude_end": exclude_end,
        "win_sec": win_sec, "fq_lim": fq_lim,
        "peak_reject": peak_reject, "peak_threshold": peak_threshold,
        "peak_margin_ms": peak_margin_ms,
    }


def filter_files(files: dict[str, FileData], cfg: dict) -> dict[str, FileData]:
    """サイドバー条件で表示対象ファイルを絞り込む。"""
    result = {}
    for k, fd in files.items():
        if fd.status == "Error" or fd.is_fft_csv:
            continue
        if cfg["sel_subjects"] and fd.subject not in cfg["sel_subjects"]:
            continue
        if cfg["sel_tasks"] and fd.task not in cfg["sel_tasks"]:
            continue
        if cfg["sel_trials"] and fd.trial not in cfg["sel_trials"]:
            continue
        if cfg["sel_phases"] and fd.phase not in cfg["sel_phases"]:
            continue
        result[k] = fd
    return result


def get_active_channels(fd: FileData, cfg: dict) -> list[ChannelSetting]:
    """サイドバー設定で有効化されているチャンネル一覧。"""
    active = []
    for cs in fd.channel_settings:
        if cs.display_name in cfg["excl_channels"]:
            continue
        if cfg["sel_channels"] and cs.display_name not in cfg["sel_channels"]:
            continue
        active.append(cs)
    return active


# =====================================================================
# Tab 1: File Overview
# =====================================================================

def tab_overview(state: dict, cfg: dict) -> None:
    st.header("📁 File Overview")

    if not state["files"]:
        st.info("サイドバーから CSV ファイルをアップロードしてください。")
        return

    # 一覧テーブル — 表示時は Order 昇順で並べる
    ordered_items = sorted(state["files"].items(), key=lambda kv: (getattr(kv[1], "order", 0) or 999999, kv[0]))
    rows = []
    for k, fd in ordered_items:
        ord_val = getattr(fd, "order", 0)
        rows.append({
            "Order": ord_val if ord_val > 0 else None,
            "File": fd.file_name,
            "Subject": fd.subject,
            "Task": fd.task,
            "Trial": fd.trial,
            "Phase": fd.phase,
            "Memo": fd.memo,
            "Duration[s]": round(fd.duration, 2),
            "SamplingRate[Hz]": fd.sampling_rate if fd.sampling_rate else None,
            "Unit": fd.unit,
            "Channels": len(fd.channel_list),
            "Status": fd.status,
        })
    df_view = pd.DataFrame(rows)

    edited = st.data_editor(
        df_view, hide_index=True, use_container_width=True,
        disabled=["File", "Duration[s]", "Channels", "Status"],
        column_config={
            "Order": st.column_config.NumberColumn(
                "Order", help="グラフ表示順 (1 から付ける)。全タブのバーグラフがこの順序で並びます。",
                min_value=1, step=1, format="%d",
            ),
            "Phase": st.column_config.SelectboxColumn(
                "Phase", options=["none", "before", "after", "pre", "post"], required=False,
            ),
        },
        key="file_overview_editor",
    )
    # 書き戻す
    for _, row in edited.iterrows():
        fd = state["files"].get(row["File"])
        if fd is None:
            continue
        fd.subject = str(row["Subject"]) if pd.notna(row["Subject"]) else ""
        fd.task = str(row["Task"]) if pd.notna(row["Task"]) else "Unknown"
        fd.trial = str(row["Trial"]) if pd.notna(row["Trial"]) else ""
        fd.phase = str(row["Phase"]) if pd.notna(row["Phase"]) else "none"
        fd.memo = str(row["Memo"]) if pd.notna(row["Memo"]) else ""
        if pd.notna(row.get("Order")):
            try:
                fd.order = int(row["Order"])
            except Exception:
                pass
        if pd.notna(row["SamplingRate[Hz]"]):
            try:
                fd.sampling_rate = float(row["SamplingRate[Hz]"])
                if fd.raw_dataframe is not None:
                    n = len(fd.raw_dataframe)
                    fd.duration = n / fd.sampling_rate if fd.sampling_rate else 0.0
                    fd.time_array = np.arange(n) / fd.sampling_rate if fd.sampling_rate else None
            except Exception:
                pass

    # === 一括並び替え ===
    with st.expander("🔢 Bulk reorder files (一括並び替え)", expanded=False):
        st.caption(
            "ファイル名を **表示したい順番に1行ずつ** 並べてください。拡張子はあってもなくても OK。"
            "ここで設定した順序は、全タブの棒グラフに反映されます。"
        )
        valid_files = [fd for fd in state["files"].values() if not fd.is_fft_csv and fd.status != "Error"]
        valid_files.sort(key=lambda f: (getattr(f, "order", 0) or 999999, f.file_name))
        current_lines = "\n".join(fd.file_name for fd in valid_files)
        new_order_text = st.text_area(
            "Order (1 file per line)", value=current_lines,
            height=max(28 * len(valid_files) + 30, 120), key="bulk_order_text",
            help="例: Miyu-Open-1.CSV / Miyu-Close-1.CSV / Miyu-Open-2.CSV / Miyu-Close-2.CSV ... のように書くと、Open と Close が交互に並びます。",
        )
        col_o1, col_o2, col_o3 = st.columns([1, 1, 2])
        with col_o1:
            if st.button("Apply order", type="primary", key="apply_order"):
                tokens = [s.strip() for s in new_order_text.splitlines() if s.strip()]
                # 完全一致 → 拡張子無視一致 の順で照合
                name_set = {fd.file_name: fd for fd in state["files"].values()}
                name_set_noext = {display_file_name(fd.file_name): fd for fd in state["files"].values()}
                applied = 0
                for i, tok in enumerate(tokens, start=1):
                    fd = name_set.get(tok) or name_set_noext.get(display_file_name(tok))
                    if fd:
                        fd.order = i
                        applied += 1
                st.success(f"{applied} ファイルに順序を適用しました")
                st.rerun()
        with col_o2:
            if st.button("Auto-alternate (Task)", key="auto_alt"):
                # Task ごとにグループ化して、各 Task の i 番目を交互に並べる
                by_task: dict[str, list[FileData]] = {}
                for fd in valid_files:
                    by_task.setdefault(fd.task, []).append(fd)
                for lst in by_task.values():
                    lst.sort(key=lambda f: (f.trial, f.file_name))
                max_len = max((len(v) for v in by_task.values()), default=0)
                order_counter = 0
                for i in range(max_len):
                    for task in sorted(by_task):
                        if i < len(by_task[task]):
                            order_counter += 1
                            by_task[task][i].order = order_counter
                st.success("Task ごとの i 番目で交互に並べました")
                st.rerun()
        with col_o3:
            if st.button("Reset (alphabetical)", key="reset_order"):
                for i, fd in enumerate(sorted(valid_files, key=lambda f: f.file_name), start=1):
                    fd.order = i
                st.rerun()

    # 警告類
    fs_set = {fd.sampling_rate for fd in state["files"].values() if fd.sampling_rate}
    if len(fs_set) > 1:
        st.warning(f"⚠️ Sampling Rate が異なるファイルがあります: {sorted(fs_set)}")
    short_files = [fd.file_name for fd in state["files"].values() if 0 < fd.duration < 30]
    if short_files:
        st.warning(f"⚠️ 30秒未満の短いファイル: {short_files}")
    fs_missing = [fd.file_name for fd in state["files"].values() if fd.sampling_rate is None and fd.status != "Error" and not fd.is_fft_csv]
    if fs_missing:
        st.error(f"❌ Sampling Rate が取得できていません: {fs_missing} (上の表で手動入力してください)")
    err_files = [(fd.file_name, fd.error) for fd in state["files"].values() if fd.status == "Error"]
    if err_files:
        st.error("読み込み失敗ファイル:")
        for name, e in err_files:
            st.write(f"- {name}: {e}")

    st.markdown("---")
    st.subheader("Filename rule")
    st.caption("ファイル名から Subject / Task / Trial / Phase を推定するためのルール。後から手動修正できます。")

    rule = state["filename_rule"]
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        delim_choice = st.selectbox(
            "Delimiter", ["-", "_", "space", "custom"],
            index=["-", "_", "space", "custom"].index(rule.get("delimiter", "-")) if rule.get("delimiter") in ["-", "_", "space"] else 3,
        )
        if delim_choice == "custom":
            delim = st.text_input("Custom delimiter", value=rule.get("delimiter", "-"))
        else:
            delim = delim_choice
    with col2:
        sidx = st.number_input("Subject pos (1-based, 0=skip)", 0, 10, int(rule.get("subject_index", 1)))
    with col3:
        tidx = st.number_input("Task pos (0=skip)", 0, 10, int(rule.get("task_index", 2)))
    with col4:
        rridx = st.number_input("Trial pos (0=skip)", 0, 10, int(rule.get("trial_index", 3)))
    with col5:
        pauto = st.checkbox("Phase auto", value=bool(rule.get("phase_auto", True)))

    if st.button("Re-estimate metadata from filenames"):
        new_rule = {
            "delimiter": delim, "subject_index": int(sidx),
            "task_index": int(tidx), "trial_index": int(rridx),
            "phase_auto": bool(pauto),
        }
        state["filename_rule"] = new_rule
        for fd in state["files"].values():
            if fd.is_fft_csv:
                continue
            est = estimate_metadata(fd.file_name, new_rule)
            fd.subject = est["subject"]
            fd.task = est["task"]
            fd.trial = est["trial"]
            fd.phase = est["phase"]
        st.success("メタデータを再推定しました")
        st.rerun()

    # JSON 保存/読み込み
    col_a, col_b = st.columns(2)
    with col_a:
        st.download_button(
            "💾 Save rule JSON",
            data=json.dumps({
                "delimiter": delim, "subject_index": int(sidx),
                "task_index": int(tidx), "trial_index": int(rridx),
                "phase_auto": bool(pauto),
            }, ensure_ascii=False, indent=2),
            file_name="filename_rule_settings.json",
            mime="application/json",
        )
    with col_b:
        up = st.file_uploader("📂 Load rule JSON", type=["json"], key="rule_json_upload")
        if up is not None:
            try:
                loaded = json.loads(up.getvalue().decode("utf-8"))
                state["filename_rule"] = loaded
                st.success("ルールを読み込みました。再推定するには上の Re-estimate ボタンを押してください。")
            except Exception as e:
                st.error(f"JSON 読み込みエラー: {e}")


# =====================================================================
# Tab 2: Channel Settings
# =====================================================================

def tab_channel_settings(state: dict, cfg: dict) -> None:
    st.header("⚙️ Channel Settings")

    if not state["files"]:
        st.info("ファイルを読み込むとチャンネル設定が表示されます。")
        return

    file_names = [k for k, fd in state["files"].items() if not fd.is_fft_csv and fd.status != "Error"]
    if not file_names:
        st.warning("有効なファイルがありません。")
        return

    target = st.selectbox("Target file", file_names, key="ch_target")
    fd = state["files"][target]

    # 全ファイル間で Display Name が一致しているかチェック
    name_map: dict[str, set[str]] = {}
    for other in state["files"].values():
        if other.is_fft_csv:
            continue
        for cs in other.channel_settings:
            name_map.setdefault(cs.original_name, set()).add(cs.display_name)
    mismatched = {k: v for k, v in name_map.items() if len(v) > 1}
    if mismatched:
        st.warning(
            "⚠️ **ファイル間で Display Name が一致していません。** これがあると比較グラフでファイルが別チャンネル行に分かれて表示されません。"
            "下の `Apply to all files` を ON のまま編集するか、`Copy settings → all files` で全ファイルに同期してください。\n\n"
            + "  \n".join(f"- Original `{k}` → {sorted(v)}" for k, v in mismatched.items())
        )

    propagate = st.checkbox(
        "🔁 Apply changes to all files (recommended)",
        value=True, key=f"prop_{target}",
        help="ON にすると、ここで変更した Display Name / Channel Type / Use for EEG Average / Memo が、同じ Original Channel Name を持つ全ファイルにも自動反映されます。"
    )

    # === プリセット適用 ===
    st.markdown("### 🎯 Preset (プリセット)")
    st.caption("よく使うチャンネル構成をワンクリックで適用できます。Original Channel Name の順番にプリセット定義を割り当てます。")
    preset_names = list(CHANNEL_PRESETS.keys())
    col_p1, col_p2 = st.columns([4, 1])
    with col_p1:
        sel_preset = st.selectbox("Preset", preset_names, key=f"preset_sel_{target}")
    with col_p2:
        if st.button("Apply preset", key=f"preset_apply_{target}", type="primary"):
            pairs = CHANNEL_PRESETS[sel_preset]
            for i, cs in enumerate(fd.channel_settings):
                if i >= len(pairs):
                    break
                display, ch_type = pairs[i]
                cs.display_name = display
                cs.channel_type = ch_type
                cs.use_for_eeg_average = (ch_type == "EEG")
                # 全ファイルに反映
                if propagate:
                    for other_fd in state["files"].values():
                        if other_fd is fd or other_fd.is_fft_csv:
                            continue
                        for other_cs in other_fd.channel_settings:
                            if other_cs.original_name == cs.original_name:
                                other_cs.display_name = display
                                other_cs.channel_type = ch_type
                                other_cs.use_for_eeg_average = (ch_type == "EEG")
            st.success(f"プリセット「{sel_preset}」を{'全ファイル' if propagate else '当ファイル'}に適用しました")
            st.rerun()
    # プリセット内容のプレビュー
    with st.expander("📋 プリセット内容", expanded=False):
        for name, pairs in CHANNEL_PRESETS.items():
            preview = ", ".join(f"{i+1}={n}({t})" for i, (n, t) in enumerate(pairs))
            st.write(f"**{name}**: {preview}")

    st.markdown("---")
    st.info("📝 **Display Name** のセルをクリックすると入力できます。`1`, `2` のような仮名を `Fp1`, `Fp2` などに変更してください。")

    rows = [{
        "Original Channel Name": cs.original_name,
        "Display Name": cs.display_name,
        "Channel Type": cs.channel_type,
        "Use for EEG Average": cs.use_for_eeg_average,
        "Memo": cs.memo,
    } for cs in fd.channel_settings]

    edited = st.data_editor(
        pd.DataFrame(rows),
        hide_index=True, use_container_width=True,
        disabled=["Original Channel Name"],
        column_config={
            "Original Channel Name": st.column_config.TextColumn("Original Channel Name", help="CSV内の元の列名 (固定・編集不可)"),
            "Display Name": st.column_config.TextColumn("Display Name ✏️", help="クリックして自由に入力できます (例: Fp1, Fp2, O1, O2)", required=False),
            "Channel Type": st.column_config.SelectboxColumn("Channel Type", options=CHANNEL_TYPES),
            "Use for EEG Average": st.column_config.CheckboxColumn("Use for EEG Average"),
            "Memo": st.column_config.TextColumn("Memo", help="電極位置などのメモ"),
        },
        key=f"channel_editor_{target}",
    )
    for i, row in edited.iterrows():
        if i < len(fd.channel_settings):
            cs = fd.channel_settings[i]
            cs.display_name = str(row["Display Name"]) if pd.notna(row["Display Name"]) and str(row["Display Name"]).strip() else cs.original_name
            cs.channel_type = str(row["Channel Type"])
            cs.use_for_eeg_average = bool(row["Use for EEG Average"])
            cs.memo = str(row["Memo"]) if pd.notna(row["Memo"]) else ""
            # 他ファイルの同じ Original Name のチャンネルにも反映
            if propagate:
                for other_fd in state["files"].values():
                    if other_fd is fd or other_fd.is_fft_csv:
                        continue
                    for other_cs in other_fd.channel_settings:
                        if other_cs.original_name == cs.original_name:
                            other_cs.display_name = cs.display_name
                            other_cs.channel_type = cs.channel_type
                            other_cs.use_for_eeg_average = cs.use_for_eeg_average
                            other_cs.memo = cs.memo

    # === 一括リネーム ===
    st.markdown("---")
    st.subheader("⚡ Bulk rename (一括リネーム)")
    st.caption(
        "上から順に Display Name を一気に書き換えたい時に使ってください。"
        "カンマ区切りまたは改行区切りで入力します。空にした行はそのチャンネルをスキップ。"
    )
    current_names = "\n".join(cs.display_name for cs in fd.channel_settings)
    new_names_text = st.text_area(
        f"Display names (順番は元のチャンネル順、{len(fd.channel_settings)} 行)",
        value=current_names,
        height=max(28 * len(fd.channel_settings) + 30, 120),
        key=f"bulk_rename_{target}",
        help="例: 1行目に Fp1、2行目に Fp2、…のように書く。改行 / カンマ どちらでも区切れます。",
    )

    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button("Apply bulk rename", key=f"bulk_apply_{target}", type="primary"):
            # 改行とカンマの両方を区切りとして扱う
            tokens = [s.strip() for s in re.split(r"[,\n]", new_names_text)]
            for i, name in enumerate(tokens):
                if i >= len(fd.channel_settings):
                    break
                if name:  # 空欄はスキップ (既存値を保持)
                    cs = fd.channel_settings[i]
                    cs.display_name = name
                    # 全ファイルに反映
                    if propagate:
                        for other_fd in state["files"].values():
                            if other_fd is fd or other_fd.is_fft_csv:
                                continue
                            for other_cs in other_fd.channel_settings:
                                if other_cs.original_name == cs.original_name:
                                    other_cs.display_name = name
            st.success(f"一括リネームを{'全ファイル' if propagate else '当ファイル'}に適用しました")
            st.rerun()
    with col_b:
        st.caption(
            "**プリセット例:**  \n"
            "国際10-20系 7ch:  `Fp1, Fp2, T3, T4, O1, O2, Cz`  \n"
            "前頭中心:  `Fp1, Fp2, F3, F4, C3, C4, Cz`"
        )

    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "💾 Save this file's channel settings (JSON)",
            data=json.dumps(serialize_channel_settings(fd), ensure_ascii=False, indent=2),
            file_name=f"{safe_filename(fd.file_name)}_channel_settings.json",
            mime="application/json",
        )
    with col2:
        up = st.file_uploader("📂 Load channel settings JSON", type=["json"], key=f"ch_json_{target}")
        if up is not None:
            try:
                data = json.loads(up.getvalue().decode("utf-8"))
                loaded = deserialize_channel_settings(data)
                # original_name の一致するものだけ適用
                by_name = {cs.original_name: cs for cs in loaded}
                for cs in fd.channel_settings:
                    if cs.original_name in by_name:
                        src = by_name[cs.original_name]
                        cs.display_name = src.display_name
                        cs.channel_type = src.channel_type
                        cs.use_for_eeg_average = src.use_for_eeg_average
                        cs.memo = src.memo
                st.success("適用しました")
                st.rerun()
            except Exception as e:
                st.error(f"JSON 読み込みエラー: {e}")
    with col3:
        if st.button("Copy settings → all files"):
            template = {cs.original_name: cs for cs in fd.channel_settings}
            for other in state["files"].values():
                if other is fd or other.is_fft_csv:
                    continue
                for cs in other.channel_settings:
                    if cs.original_name in template:
                        src = template[cs.original_name]
                        cs.display_name = src.display_name
                        cs.channel_type = src.channel_type
                        cs.use_for_eeg_average = src.use_for_eeg_average
                        cs.memo = src.memo
            st.success("全ファイルに反映しました")

    # 解析対象まとめ
    eeg_avg = [cs.display_name for cs in fd.channel_settings if cs.channel_type == "EEG" and cs.use_for_eeg_average]
    excluded = [cs.display_name for cs in fd.channel_settings if cs.channel_type in ("Exclude",) or not cs.use_for_eeg_average]
    st.write("**EEG Average に使うチャンネル:**", ", ".join(eeg_avg) if eeg_avg else "(なし)")
    st.write("**EEG Average から除外:**", ", ".join(excluded) if excluded else "(なし)")


# =====================================================================
# Tab 3: Raw / Filtered Waveform
# =====================================================================

def tab_waveform(state: dict, cfg: dict) -> None:
    st.header("📈 Raw / Filtered Waveform")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    sel_files = st.multiselect("Files", list(files.keys()), default=list(files.keys())[:1], key="wave_files")
    mode = st.radio("Mode", ["Raw", "Filtered" if cfg["filt_on"] else "Raw only"], horizontal=True, key="wave_mode")
    if not cfg["filt_on"] and mode != "Raw":
        st.info("追加フィルタが OFF のため Raw を表示します。")
        mode = "Raw"

    for fname in sel_files:
        fd = files[fname]
        st.subheader(f"{fname}  (fs={fd.sampling_rate} Hz, dur={fd.duration:.1f}s)")
        active = get_active_channels(fd, cfg)
        if not active:
            st.info("有効なチャンネルがありません。")
            continue
        fig = make_subplots(rows=len(active), cols=1, shared_xaxes=True, vertical_spacing=0.02)
        for i, cs in enumerate(active, start=1):
            sig_arr, t_axis = get_processed_signal(
                fd, cs.original_name, filt_on=(mode == "Filtered"),
                hpf=cfg["hpf"], lpf=cfg["lpf"], notch=cfg["notch"],
                exclude_start=cfg["exclude_start"], exclude_end=cfg["exclude_end"],
                peak_reject=cfg.get("peak_reject", False),
                peak_threshold=cfg.get("peak_threshold", 75.0),
                peak_margin_ms=cfg.get("peak_margin_ms", 100.0),
            )
            fig.add_trace(go.Scatter(
                x=t_axis, y=sig_arr, mode="lines",
                name=f"{cs.display_name} ({cs.original_name})",
                hovertemplate=f"t=%{{x:.2f}}s<br>{cs.display_name}=%{{y:.2f}}<extra></extra>",
                showlegend=False,
            ), row=i, col=1)
            fig.update_yaxes(title_text=cs.display_name, row=i, col=1)
        fig.update_xaxes(title_text="Time [s]", row=len(active), col=1)
        fig.update_layout(height=max(150 * len(active), 300), margin=dict(l=40, r=20, t=20, b=40))
        st.plotly_chart(fig, use_container_width=True)


# =====================================================================
# Tab 4: FFT / Spectrogram
# =====================================================================

def compute_file_spectrograms(fd: FileData, cfg: dict, channels: Optional[list[ChannelSetting]] = None) -> dict[str, dict]:
    """1ファイルの選択チャンネルそれぞれについて spectrogram を計算。
    Returns: {display_name: {spctgram, freq, t, original}}
    """
    out: dict[str, dict] = {}
    if fd.sampling_rate is None or fd.raw_dataframe is None:
        return out
    chs = channels if channels is not None else get_active_channels(fd, cfg)
    for cs in chs:
        sig_arr, _ = get_processed_signal(
            fd, cs.original_name, filt_on=cfg["filt_on"],
            hpf=cfg["hpf"], lpf=cfg["lpf"], notch=cfg["notch"],
            exclude_start=cfg["exclude_start"], exclude_end=cfg["exclude_end"],
            peak_reject=cfg.get("peak_reject", False),
            peak_threshold=cfg.get("peak_threshold", 75.0),
            peak_margin_ms=cfg.get("peak_margin_ms", 100.0),
        )
        if len(sig_arr) < int(cfg["win_sec"] * fd.sampling_rate):
            continue
        sp, freq, t = compute_spectrogram(sig_arr, fd.sampling_rate, cfg["win_sec"], cfg["fq_lim"])
        out[cs.display_name] = {"spctgram": sp, "freq": freq, "t": t, "channel": cs}
    return out


def tab_fft(state: dict, cfg: dict) -> None:
    st.header("🌈 FFT / Spectrogram")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    sel_file = st.selectbox("File", list(files.keys()), key="fft_file")
    fd = files[sel_file]
    if fd.sampling_rate is None:
        st.error("Sampling Rate が未設定です。File Overview で入力してください。")
        return

    active = get_active_channels(fd, cfg)
    if not active:
        st.info("有効なチャンネルがありません。")
        return
    sel_ch_name = st.selectbox("Channel", [cs.display_name for cs in active], key="fft_channel")
    cs_sel = next(cs for cs in active if cs.display_name == sel_ch_name)

    specs = compute_file_spectrograms(fd, cfg, channels=[cs_sel])
    if sel_ch_name not in specs:
        st.warning("解析対象データが短すぎます。Exclude 設定や FFT 窓幅を見直してください。")
        return

    sp = specs[sel_ch_name]["spctgram"]
    freq = specs[sel_ch_name]["freq"]
    t = specs[sel_ch_name]["t"]

    # Spectrogram
    st.subheader("Spectrogram (PSD)")
    fig_sp = go.Figure(data=go.Heatmap(
        z=10 * np.log10(np.maximum(sp, 1e-12)), x=t, y=freq, colorscale="Jet",
        colorbar=dict(title="dB (10·log10 PSD)"),
    ))
    fig_sp.update_layout(
        xaxis_title="Time [s]", yaxis_title="Frequency [Hz]",
        title=f"{sel_file} / {sel_ch_name} (orig: {cs_sel.original_name})",
        height=400,
    )
    st.plotly_chart(fig_sp, use_container_width=True)

    # 平均PSD
    st.subheader("Mean PSD (time-averaged)")
    mean_psd = sp.mean(axis=1)
    fig_psd = go.Figure()
    fig_psd.add_trace(go.Scatter(x=freq, y=mean_psd, mode="lines", name=sel_ch_name))
    fig_psd.update_layout(xaxis_title="Frequency [Hz]", yaxis_title="PSD [uV^2/Hz]", height=320, yaxis_type="log")
    st.plotly_chart(fig_psd, use_container_width=True)

    # 選択チャンネル平均PSD (EEG 平均)
    st.subheader("EEG Average PSD (Channel Type=EEG & Use for EEG Average=ON)")
    eeg_chs = [cs for cs in active if cs.channel_type == "EEG" and cs.use_for_eeg_average]
    if eeg_chs:
        all_specs = compute_file_spectrograms(fd, cfg, channels=eeg_chs)
        if all_specs:
            ref_freq = next(iter(all_specs.values()))["freq"]
            stack = np.stack([d["spctgram"].mean(axis=1) for d in all_specs.values()], axis=0)
            avg = stack.mean(axis=0)
            fig_avg = go.Figure()
            for name, d in all_specs.items():
                fig_avg.add_trace(go.Scatter(x=d["freq"], y=d["spctgram"].mean(axis=1), mode="lines", name=name, opacity=0.4))
            fig_avg.add_trace(go.Scatter(x=ref_freq, y=avg, mode="lines", name="EEG Mean", line=dict(width=3, color="black")))
            fig_avg.update_layout(xaxis_title="Frequency [Hz]", yaxis_title="PSD [uV^2/Hz]", height=320, yaxis_type="log")
            st.plotly_chart(fig_avg, use_container_width=True)
    else:
        st.info("EEG 平均対象チャンネルがありません。")


# =====================================================================
# Tab 5: Band Power
# =====================================================================

def tab_band_power(state: dict, cfg: dict) -> None:
    st.header("🎚 Band Power")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    rows = []
    timeseries: dict[str, pd.DataFrame] = {}
    for fname, fd in files.items():
        if fd.sampling_rate is None:
            continue
        for cs in get_active_channels(fd, cfg):
            specs = compute_file_spectrograms(fd, cfg, channels=[cs])
            if cs.display_name not in specs:
                continue
            d = specs[cs.display_name]
            bp = compute_band_power_table(d["spctgram"], d["freq"], d["t"])
            mean_psd = d["spctgram"].mean(axis=1)
            apf = alpha_peak_frequency(mean_psd, d["freq"])
            row = {"File": fname, "Subject": fd.subject, "Task": fd.task, "Trial": fd.trial, "Phase": fd.phase, "Channel": cs.display_name, "Type": cs.channel_type}
            for band in BAND_DEFS:
                row[band] = float(bp[band].mean())
            row["Alpha/Total"] = float(np.nanmean(bp["Alpha/Total"]))
            row["Theta/Beta"] = float(np.nanmean(bp["Theta/Beta"]))
            row["Beta/Alpha"] = float(np.nanmean(bp["Beta/Alpha"]))
            row["AlphaPeakFreq"] = apf
            rows.append(row)
            timeseries[f"{fname}|{cs.display_name}"] = bp

    if not rows:
        st.warning("帯域パワーを計算できませんでした (解析対象区間が短い可能性)。")
        return

    df = pd.DataFrame(rows)
    st.subheader("Per-file / Per-channel band power (mean over time)")
    st.dataframe(df, use_container_width=True)

    st.subheader("Bar plot")
    band_pick = st.selectbox("Band", ["Delta", "Theta", "Alpha", "Beta", "Gamma", "Alpha1", "Alpha2", "Beta1", "Beta2", "Gamma1", "Gamma2", "Alpha/Total", "Theta/Beta", "Beta/Alpha"], key="bp_band")
    fig = px.bar(df, x="File", y=band_pick, color="Channel", barmode="group", hover_data=["Subject", "Task", "Phase"])
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Time series of band power")
    ts_key = st.selectbox("Series", list(timeseries.keys()), key="bp_series")
    bp = timeseries[ts_key]
    bands_show = st.multiselect("Bands", ["Delta", "Theta", "Alpha", "Beta", "Gamma"], default=["Theta", "Alpha", "Beta"], key="bp_bands_show")
    fig_ts = go.Figure()
    for b in bands_show:
        fig_ts.add_trace(go.Scatter(x=bp["Time"], y=bp[b], name=b, mode="lines"))
    fig_ts.update_layout(xaxis_title="Time [s]", yaxis_title="PSD [uV^2/Hz]", yaxis_type="log", height=380)
    st.plotly_chart(fig_ts, use_container_width=True)

    # 保存用に session_state へ
    state["_band_power_table"] = df


# =====================================================================
# Tab 6: Condition Comparison
# =====================================================================

def tab_condition_comparison(state: dict, cfg: dict) -> None:
    """Per-condition Summary: 任意のメタ項目で全ファイルを表形式に並べる。

    2条件の比較ではなく、アップロードしたファイル数だけ行 (or 棒) が並ぶ。
    """
    st.header("📊 Per-file / Per-condition Summary")
    st.caption("2条件A vs B ではなく、アップロードした全ファイルを並べてサマリーします。条件は色分けに使う任意の軸です。")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    # 全ファイルについて、各種メトリクスを計算
    rows = []
    for fname, fd in files.items():
        if fd.sampling_rate is None:
            continue
        for cs in get_active_channels(fd, cfg):
            specs = compute_file_spectrograms(fd, cfg, channels=[cs])
            if cs.display_name not in specs:
                continue
            d = specs[cs.display_name]
            bp = compute_band_power_table(d["spctgram"], d["freq"], d["t"])
            mean_psd = d["spctgram"].mean(axis=1)
            sig_arr, _ = get_processed_signal(
                fd, cs.original_name, filt_on=cfg["filt_on"],
                hpf=cfg["hpf"], lpf=cfg["lpf"], notch=cfg["notch"],
                exclude_start=cfg["exclude_start"], exclude_end=cfg["exclude_end"],
                peak_reject=cfg.get("peak_reject", False),
                peak_threshold=cfg.get("peak_threshold", 75.0),
                peak_margin_ms=cfg.get("peak_margin_ms", 100.0),
            )
            row = {
                "File": fname, "FileLabel": display_file_name(fname),
                "Subject": fd.subject, "Task": fd.task,
                "Trial": fd.trial, "Phase": fd.phase, "Channel": cs.display_name,
                "Type": cs.channel_type,
            }
            for band in ["Delta", "Theta", "Alpha1", "Alpha2", "Alpha", "Beta1", "Beta2", "Beta", "Gamma1", "Gamma2", "Gamma"]:
                row[band] = float(bp[band].mean())
            row["Alpha/Total"] = float(np.nanmean(bp["Alpha/Total"]))
            row["Theta/Beta"] = float(np.nanmean(bp["Theta/Beta"]))
            row["Beta/Alpha"] = float(np.nanmean(bp["Beta/Alpha"]))
            row["AlphaPeakFreq"] = alpha_peak_frequency(mean_psd, d["freq"])
            row["peak-to-peak"] = float(np.ptp(sig_arr)) if len(sig_arr) else np.nan
            row["std"] = float(np.std(sig_arr)) if len(sig_arr) else np.nan
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        st.warning("値を計算できませんでした。")
        return

    st.subheader(f"All files × channels  (N files = {df['File'].nunique()})")
    st.dataframe(df, use_container_width=True)
    state["_condition_comparison"] = df

    # 棒グラフ表示
    st.subheader("Bar plot — each bar is one file")
    metrics = ["Beta/Alpha", "Alpha", "Beta", "Gamma", "Delta", "Theta", "Alpha1", "Alpha2", "Beta1", "Beta2", "Gamma1", "Gamma2", "Alpha/Total", "Theta/Beta", "AlphaPeakFreq", "peak-to-peak", "std"]
    col1, col2, col3 = st.columns(3)
    with col1:
        metric_pick = st.selectbox("Metric", metrics, key="cmp_metric")
    with col2:
        color_by = st.selectbox("Color by", ["Task", "Phase", "Subject", "Trial", "(none)"], index=0, key="cmp_color")
    with col3:
        channel_pick = st.selectbox("Channel filter", ["(all)"] + sorted(df["Channel"].unique()), key="cmp_chfilter")

    plot_df = df if channel_pick == "(all)" else df[df["Channel"] == channel_pick]
    color_arg = None if color_by == "(none)" else color_by
    n_ch = plot_df["Channel"].nunique() if channel_pick == "(all)" else 1
    order_list = get_file_order_labels(state)
    plot_df = apply_file_order(plot_df, order_list, "FileLabel")
    cat_orders = {"FileLabel": order_list}
    # Debug: 現在の順序を画面に表示
    st.caption(f"📋 Current display order: {' → '.join(order_list)}")
    if channel_pick == "(all)":
        spacing_frac, total_h = compute_facet_layout(n_ch)
        fig = px.bar(
            plot_df, x="FileLabel", y=metric_pick, color=color_arg, barmode="group",
            facet_row="Channel",
            facet_row_spacing=spacing_frac,
            category_orders=cat_orders,
            hover_data=["File", "Subject", "Task", "Phase", "Trial"],
            title=f"{metric_pick} — each bar = one file",
        )
        fig.update_layout(height=total_h, margin=dict(l=60, r=20, t=60, b=120), bargap=0.2)
    else:
        fig = px.bar(
            plot_df, x="FileLabel", y=metric_pick, color=color_arg,
            category_orders=cat_orders,
            hover_data=["File", "Subject", "Task", "Phase", "Trial", "Channel"],
            title=f"{metric_pick} on {channel_pick} — each bar = one file",
        )
        fig.update_layout(height=520, margin=dict(l=60, r=20, t=60, b=120), bargap=0.2)
    fig.update_xaxes(
        showticklabels=True, tickangle=0, title_text="", automargin=True, matches=None,
        categoryorder="array", categoryarray=order_list,
    )
    fig.update_yaxes(matches=None, autorange=True, automargin=True)
    st.plotly_chart(fig, use_container_width=True)

    # ピボット表 (File × Channel)
    st.subheader("Pivot (File × Channel)")
    pivot = df.pivot_table(index="File", columns="Channel", values=metric_pick, aggfunc="mean")
    st.dataframe(pivot, use_container_width=True)


# =====================================================================
# Tab 7: Open vs Close
# =====================================================================

def tab_group_compare(state: dict, cfg: dict) -> None:
    """全ファイルを個別の棒として並べる多ファイル比較タブ。

    ファイル間で平均は取らない。8 ファイルあれば 8 ファイル分の値を並べる。
    色分け (Color by) で Task / Phase / Subject などをグルーピング表示できる。
    """
    st.header("🔀 Multi-file Comparison")
    st.caption("アップロードした各ファイルを個別の棒として並べます。ファイル間で平均は取りません。")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    # 比較対象ファイル
    sel_files = st.multiselect("Files to compare", list(files.keys()), default=list(files.keys()), key="mc_files")
    if not sel_files:
        st.info("ファイルを1つ以上選択してください。")
        return

    # 色分けキー (グルーピング用) と、X軸の主軸
    col1, col2, col3 = st.columns(3)
    with col1:
        color_by = st.selectbox("Color by", ["Task", "Phase", "Subject", "Trial", "(none)"], index=0, key="mc_color")
    with col2:
        x_axis = st.selectbox("X axis", ["File", "Channel"], index=0, key="mc_x")
    with col3:
        metric = st.selectbox(
            "Metric",
            ["Beta/Alpha", "Alpha", "Beta", "Gamma", "Delta", "Theta", "Alpha1", "Alpha2", "Beta1", "Beta2", "Gamma1", "Gamma2", "Alpha/Total", "Theta/Beta", "AlphaPeakFreq"],
            index=0, key="mc_metric",
            help="Beta/Alpha は脳の活性指標 (大きいほど活性、小さいほどリラックス傾向)。あくまで信号解析上の目安です。",
        )

    # チャンネル絞り込み (オプション)
    all_ch = sorted({cs.display_name for k in sel_files for cs in get_active_channels(files[k], cfg)})
    sel_chs = st.multiselect("Channels to show", all_ch, default=all_ch, key="mc_channels")
    if not sel_chs:
        st.info("チャンネルを1つ以上選択してください。")
        return

    st.caption(
        f"集計方法: 各ファイル × 各チャンネルについて時間平均した {metric} を 1 値として算出し、ファイル間で平均は取らずに並べます。"
    )

    # 全メトリクスを一度に計算 (画面表示は metric だけだが Export 用に全部持つ)
    all_metrics = ["Delta", "Theta", "Alpha1", "Alpha2", "Alpha", "Beta1", "Beta2", "Beta",
                   "Gamma1", "Gamma2", "Total", "Alpha/Total", "Theta/Beta", "Beta/Alpha", "AlphaPeakFreq"]
    rows = []
    for fname in sel_files:
        fd = files[fname]
        if fd.sampling_rate is None:
            continue
        for cs in get_active_channels(fd, cfg):
            if cs.display_name not in sel_chs:
                continue
            specs = compute_file_spectrograms(fd, cfg, channels=[cs])
            if cs.display_name not in specs:
                continue
            d = specs[cs.display_name]
            bp = compute_band_power_table(d["spctgram"], d["freq"], d["t"])
            mean_psd = d["spctgram"].mean(axis=1)
            row = {
                "File": fname, "FileLabel": display_file_name(fname),
                "Channel": cs.display_name,
                "Subject": fd.subject, "Task": fd.task, "Trial": fd.trial, "Phase": fd.phase,
            }
            for m in all_metrics:
                if m == "AlphaPeakFreq":
                    row[m] = float(alpha_peak_frequency(mean_psd, d["freq"]))
                elif m in bp.columns:
                    row[m] = float(np.nanmean(bp[m]))
                else:
                    row[m] = np.nan
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        st.warning("値を計算できませんでした。解析対象区間や FFT 窓幅を確認してください。")
        return

    st.subheader(f"All values — file × channel (全メトリクスを表示)")
    st.caption("📥 Export Report ZIP の `Multi-file_Comparison.csv` にもこの全メトリクスがそのまま出力されます。")
    st.dataframe(df, use_container_width=True)
    state["_group_compare"] = df

    color_arg = None if color_by == "(none)" else color_by

    # メイン棒グラフ (全ファイルを個別バーで表示)
    st.subheader(f"{metric} per file × channel")
    order_list = get_file_order_labels(state)
    df = apply_file_order(df, order_list, "FileLabel")
    cat_orders = {"FileLabel": order_list}
    st.caption(f"📋 Current display order: {' → '.join(order_list)}")
    if x_axis == "File":
        n_ch = max(len(sel_chs), 1)
        spacing_frac, total_h = compute_facet_layout(n_ch)
        fig = px.bar(
            df, x="FileLabel", y=metric, color=color_arg,
            facet_row="Channel" if len(sel_chs) > 1 else None,
            facet_row_spacing=spacing_frac,
            category_orders=cat_orders,
            hover_data=["File", "Subject", "Task", "Phase", "Trial", "Channel"],
            title=f"{metric} per file (each bar = one file)",
        )
        # 各パネルに x ラベル表示 (どのファイルか必ず分かるように) + y は独立スケール
        fig.update_yaxes(matches=None, autorange=True, automargin=True)
        fig.update_xaxes(
            showticklabels=True, tickangle=0, title_text="", automargin=True, matches=None,
            categoryorder="array", categoryarray=order_list,
        )
        fig.update_layout(
            height=total_h, margin=dict(l=60, r=20, t=60, b=120), bargap=0.2,
        )
    else:
        # X=Channel, 各ファイルが別バー (group)
        fig = px.bar(
            df, x="Channel", y=metric, color="FileLabel", barmode="group",
            category_orders=cat_orders,
            hover_data=["File", "Subject", "Task", "Phase", "Trial"],
            title=f"{metric} per channel (each bar = one file)",
        )
        fig.update_yaxes(autorange=True, automargin=True)
        fig.update_xaxes(tickangle=0, automargin=True)
        fig.update_layout(height=520, margin=dict(l=60, r=20, t=60, b=80))
    st.plotly_chart(fig, use_container_width=True)

    # ヒートマップ (file × channel)
    st.subheader("Heatmap (file × channel)")
    pivot = df.pivot_table(index="File", columns="Channel", values=metric, aggfunc="mean")
    fig_hm = px.imshow(pivot, aspect="auto", color_continuous_scale="Viridis",
                       labels=dict(color=metric))
    st.plotly_chart(fig_hm, use_container_width=True)

    # ペア比 (任意の基準ファイルとの比)
    st.markdown("---")
    st.subheader("Pairwise ratio (optional)")
    st.caption("基準ファイルを1つ選ぶと、他の全ファイルとの比 (other/baseline) を表示します。")
    baseline = st.selectbox("Baseline file", ["(none)"] + sel_files, index=0, key="mc_baseline")
    if baseline != "(none)":
        base = df[df["File"] == baseline].set_index("Channel")[metric]
        ratio_rows = []
        for fname in sel_files:
            if fname == baseline:
                continue
            sub = df[df["File"] == fname].set_index("Channel")[metric]
            for ch in sub.index:
                if ch in base.index and base[ch]:
                    ratio_rows.append({"File": fname, "Channel": ch, "Ratio": float(sub[ch] / base[ch])})
        df_ratio = pd.DataFrame(ratio_rows)
        if not df_ratio.empty:
            fig_r = px.bar(df_ratio, x="Channel", y="Ratio", color="File", barmode="group",
                           title=f"{metric} ratio (each file / {baseline})")
            fig_r.add_hline(y=1.0, line_dash="dot")
            st.plotly_chart(fig_r, use_container_width=True)
            st.dataframe(df_ratio, use_container_width=True)

    st.caption("注: 値はあくまで信号解析結果です。医学的・心理的な状態を断定するものではありません。")


# =====================================================================
# Tab 8: Before / After
# =====================================================================

def tab_paired_compare(state: dict, cfg: dict) -> None:
    """Group Comparison: ペアリングキーで束ねた中の全ファイルを並べる N-file 比較。

    2-state (A/B) ではなく、各グループ内の全ファイル (N個) を個別の棒として表示する。
    例: Subject=Miyu, Task=SDMT, Trial=1 のグループに Phase=before, mid, after の 3 ファイル
        があれば 3 本の棒を並べる。
    """
    st.header("🔗 Group Comparison (N-file per group)")
    st.caption("ペアリングキー (例: Subject+Task+Trial) で同じ値のファイルを1グループにまとめ、グループ内の全ファイル (N個) を個別の棒として並べます。2-state 比較に固定しません。")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    pair_keys = st.multiselect(
        "Pairing key (同じ値のものを1グループにする)",
        ["Subject", "Task", "Trial", "Memo"], default=["Subject", "Task", "Trial"], key="gc2_pair_keys",
    )
    diff_field = st.selectbox(
        "Distinguish files within group by (色分け)",
        ["Phase", "Task", "Memo", "Trial", "File"], index=0, key="gc2_diff_field",
    )
    diff_attr = diff_field.lower() if diff_field != "File" else "file_name"

    metric = st.selectbox(
        "Metric",
        ["Beta/Alpha", "Alpha", "Beta", "Gamma", "Delta", "Theta", "Alpha1", "Alpha2", "Beta1", "Beta2", "Gamma1", "Gamma2", "Alpha/Total", "Theta/Beta", "AlphaPeakFreq"],
        index=0, key="gc2_metric",
    )

    # グルーピング
    def key_tuple(fd: FileData) -> tuple:
        if not pair_keys:
            return ()
        return tuple(getattr(fd, k.lower(), "") for k in pair_keys)

    groups: dict[tuple, list[FileData]] = {}
    for fd in files.values():
        groups.setdefault(key_tuple(fd), []).append(fd)

    # 1ファイルしかないグループを除外するか
    only_multi = st.checkbox("Show only groups with ≥2 files", value=True, key="gc2_only_multi")
    shown_groups = {kt: fds for kt, fds in groups.items() if (not only_multi) or len(fds) >= 2}
    if not shown_groups:
        st.info("該当するグループがありません。Pairing key を変更するか、≥2 のチェックを外してください。")
        return

    # 全メトリクスを一度に計算 (画面表示は metric だけだが Export 用に全部持つ)
    all_metrics = ["Delta", "Theta", "Alpha1", "Alpha2", "Alpha", "Beta1", "Beta2", "Beta",
                   "Gamma1", "Gamma2", "Total", "Alpha/Total", "Theta/Beta", "Beta/Alpha", "AlphaPeakFreq"]
    rows = []
    for kt, fds in shown_groups.items():
        group_label = " / ".join(f"{k}={v}" for k, v in zip(pair_keys, kt) if v) or "(all)"
        for fd in fds:
            if fd.sampling_rate is None:
                continue
            for cs in get_active_channels(fd, cfg):
                specs = compute_file_spectrograms(fd, cfg, channels=[cs])
                if cs.display_name not in specs:
                    continue
                d = specs[cs.display_name]
                bp = compute_band_power_table(d["spctgram"], d["freq"], d["t"])
                mean_psd = d["spctgram"].mean(axis=1)
                row = {
                    "Group": group_label,
                    "File": fd.file_name,
                    "FileLabel": display_file_name(fd.file_name),
                    "Channel": cs.display_name,
                    "Subject": fd.subject, "Task": fd.task, "Trial": fd.trial, "Phase": fd.phase,
                    "DiffField": str(getattr(fd, diff_attr, "")),
                }
                for m in all_metrics:
                    if m == "AlphaPeakFreq":
                        row[m] = float(alpha_peak_frequency(mean_psd, d["freq"]))
                    elif m in bp.columns:
                        row[m] = float(np.nanmean(bp[m]))
                    else:
                        row[m] = np.nan
                rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        st.warning("値を計算できませんでした。")
        return

    st.write(f"検出グループ数: {df['Group'].nunique()}  /  対象ファイル数: {df['File'].nunique()}")
    st.dataframe(df, use_container_width=True)
    state["_paired_compare"] = df

    # 各グループ × 各チャンネル を1パネルにして、グループ内の全ファイルを並べる
    st.subheader(f"{metric} — each group: all files side-by-side")
    sel_channel = st.selectbox("Channel", ["(all)"] + sorted(df["Channel"].unique()), key="gc2_ch")
    plot_df = df if sel_channel == "(all)" else df[df["Channel"] == sel_channel]
    n_ch = plot_df["Channel"].nunique() if sel_channel == "(all)" else 1
    order_list = get_file_order_labels(state)
    plot_df = apply_file_order(plot_df, order_list, "FileLabel")
    cat_orders = {"FileLabel": order_list}
    if sel_channel == "(all)":
        spacing_frac, total_h = compute_facet_layout(n_ch)
        fig = px.bar(
            plot_df, x="FileLabel", y=metric, color="DiffField",
            facet_col="Group", facet_row="Channel", facet_col_wrap=3,
            facet_row_spacing=spacing_frac,
            category_orders=cat_orders,
            hover_data=["File", "Subject", "Task", "Phase", "Trial"],
            title=f"{metric} per file, faceted by group × channel",
        )
        fig.update_layout(height=total_h, margin=dict(l=60, r=20, t=60, b=120), bargap=0.2)
    else:
        fig = px.bar(
            plot_df, x="FileLabel", y=metric, color="DiffField",
            facet_col="Group", facet_col_wrap=3,
            category_orders=cat_orders,
            hover_data=["File", "Subject", "Task", "Phase", "Trial"],
            title=f"{metric} on {sel_channel} per file, faceted by group",
        )
        fig.update_layout(height=520, margin=dict(l=60, r=20, t=60, b=120), bargap=0.2)
    fig.update_xaxes(
        showticklabels=True, tickangle=0, title_text="", automargin=True, matches=None,
        categoryorder="array", categoryarray=order_list,
    )
    fig.update_yaxes(matches=None, autorange=True, automargin=True)
    st.plotly_chart(fig, use_container_width=True)

    # グループ内の基準ファイルとの比 (オプション)
    st.markdown("---")
    st.subheader("Within-group ratio (optional)")
    st.caption("各グループ内で「基準とする DiffField の値」を選ぶと、グループ内の他ファイル ÷ 基準ファイル の比を計算します (例: Phase=before を基準にして mid, after との比を見る)。")
    diff_values = sorted({v for v in df["DiffField"].unique() if v})
    base_val = st.selectbox(f"Baseline {diff_field} value", ["(none)"] + diff_values, key="gc2_base")
    if base_val != "(none)":
        rrows = []
        for (grp, ch), sub in df.groupby(["Group", "Channel"]):
            base_rows = sub[sub["DiffField"] == base_val]
            if base_rows.empty:
                continue
            base_v = float(base_rows.iloc[0][metric])
            if not base_v:
                continue
            for _, r in sub.iterrows():
                if r["DiffField"] == base_val:
                    continue
                rrows.append({
                    "Group": grp, "Channel": ch, "File": r["File"],
                    "DiffField": r["DiffField"], "Ratio": r[metric] / base_v,
                    "Change(%)": (r[metric] - base_v) / base_v * 100.0,
                })
        df_ratio = pd.DataFrame(rrows)
        if not df_ratio.empty:
            st.dataframe(df_ratio, use_container_width=True)
            fig_r = px.bar(df_ratio, x="Channel", y="Ratio", color="DiffField", facet_col="Group", facet_col_wrap=3,
                           title=f"Ratio (other / {diff_field}={base_val}) for {metric}")
            fig_r.add_hline(y=1.0, line_dash="dot")
            st.plotly_chart(fig_r, use_container_width=True)


# =====================================================================
# Tab 9: Channel Quality
# =====================================================================

def tab_quality(state: dict, cfg: dict) -> None:
    st.header("🩺 Channel Quality")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    rows = []
    for fname, fd in files.items():
        if fd.sampling_rate is None:
            continue
        for cs in fd.channel_settings:
            sig_arr, _ = get_processed_signal(
                fd, cs.original_name, filt_on=cfg["filt_on"],
                hpf=cfg["hpf"], lpf=cfg["lpf"], notch=cfg["notch"],
                exclude_start=cfg["exclude_start"], exclude_end=cfg["exclude_end"],
                peak_reject=cfg.get("peak_reject", False),
                peak_threshold=cfg.get("peak_threshold", 75.0),
                peak_margin_ms=cfg.get("peak_margin_ms", 100.0),
            )
            if len(sig_arr) < 4:
                continue
            ptp = float(np.ptp(sig_arr))
            out100 = float(np.mean(np.abs(sig_arr) > 100.0))
            diff = np.diff(sig_arr)
            steep = float(np.mean(np.abs(diff) > 30.0))
            # 低周波ドリフト: 0-1Hz のパワー比率
            sp, freq, _ = compute_spectrogram(sig_arr, fd.sampling_rate, cfg["win_sec"], cfg["fq_lim"])
            mp = sp.mean(axis=1)
            tot = float(mp.sum()) if mp.sum() > 0 else np.nan
            drift = float(mp[(freq >= 0) & (freq < 1.0)].sum()) / tot if tot else np.nan
            line50 = float(mp[(freq >= 49) & (freq <= 51)].sum()) / tot if tot else np.nan
            line60 = float(mp[(freq >= 59) & (freq <= 61)].sum()) / tot if tot else np.nan

            quality = "Good"
            if cs.channel_type in ("ECG", "EOG", "EMG", "Other", "Exclude"):
                quality = "(reference)"
            elif ptp >= 300:
                quality = "Bad"
            elif ptp >= 100:
                quality = "Warning"

            rows.append({
                "File": fname, "Channel": cs.display_name, "Type": cs.channel_type,
                "mean": float(np.mean(sig_arr)), "std": float(np.std(sig_arr)),
                "min": float(np.min(sig_arr)), "max": float(np.max(sig_arr)),
                "peak-to-peak": ptp, "out>100uV": out100, "steep>30uV": steep,
                "lowFreqDrift": drift, "50Hz": line50, "60Hz": line60,
                "Quality": quality,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        st.warning("品質チェックを計算できませんでした。")
        return
    st.dataframe(df, use_container_width=True)
    state["_channel_quality"] = df

    st.caption("Quality は peak-to-peak < 100uV: Good / 100-300uV: Warning / >=300uV: Bad の目安です。非EEGチャンネルは参考表示。")
    st.checkbox("Future option: 自動で Bad チャンネルを比較・平均から除外する (このフラグを ON にすると、Channel Settings で Exclude にしてください)", value=False, disabled=True)

    # ヒートマップ
    pivot = df.pivot_table(index="File", columns="Channel", values="peak-to-peak", aggfunc="mean")
    fig = px.imshow(pivot, aspect="auto", color_continuous_scale="Reds", title="Peak-to-Peak heatmap (uV)")
    st.plotly_chart(fig, use_container_width=True)


# =====================================================================
# Tab 10: FFT CSV Export
# =====================================================================

def fft_csv_filename(fd: FileData, cs: ChannelSetting) -> str:
    base = re.sub(r"\.[^.]+$", "", fd.file_name)
    return f"{safe_filename(base)}_{safe_filename(cs.display_name)}_fft.csv"


def fft_meta_dict(fd: FileData, cs: ChannelSetting, cfg: dict, freq: np.ndarray) -> dict:
    return {
        "original_file_name": fd.file_name,
        "subject": fd.subject, "task": fd.task, "trial": fd.trial, "phase": fd.phase,
        "original_channel_name": cs.original_name,
        "display_channel_name": cs.display_name,
        "channel_type": cs.channel_type,
        "sampling_rate": fd.sampling_rate,
        "fft_window_seconds": cfg["win_sec"],
        "fft_overlap_seconds": max(cfg["win_sec"] - 1.0, 0.0),
        "nfft": int(next_pow2(int(fd.sampling_rate * cfg["win_sec"]))) if fd.sampling_rate else None,
        "frequency_resolution": (float(fd.sampling_rate) / next_pow2(int(fd.sampling_rate * cfg["win_sec"]))) if fd.sampling_rate else None,
        "frequency_columns_count": int(len(freq)),
    }


def tab_fft_export(state: dict, cfg: dict) -> None:
    st.header("📤 FFT CSV Export")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    st.caption("Time列 + 帯域パワー + F0, F0.29, ... のように時間窓ごとに 1 行ずつ出力します (mind_*.csv 互換)。")

    sel_files = st.multiselect("Files", list(files.keys()), default=list(files.keys()), key="exp_files")
    mode = st.radio("Output mode", [
        "Selected file & selected channel",
        "All files / all channels (ZIP)",
        "EEG channel mean per file",
        "Condition mean (by Task)",
        "Condition mean (by Task & Phase)",
    ], key="exp_mode")

    if mode == "Selected file & selected channel":
        if not sel_files:
            st.info("ファイルを選択してください。")
            return
        sel_file = st.selectbox("File", sel_files, key="exp_sel_file")
        fd = files[sel_file]
        active = get_active_channels(fd, cfg)
        if not active:
            st.info("チャンネルがありません。")
            return
        sel_ch = st.selectbox("Channel", [cs.display_name for cs in active], key="exp_sel_ch")
        cs = next(c for c in active if c.display_name == sel_ch)
        specs = compute_file_spectrograms(fd, cfg, channels=[cs])
        if sel_ch not in specs:
            st.warning("計算できませんでした。")
            return
        d = specs[sel_ch]
        export_df = build_fft_export_dataframe(d["spctgram"], d["freq"], d["t"], fd.clock_array, fd.sampling_rate or 0.0, cfg["exclude_start"])
        st.dataframe(export_df.head(20), use_container_width=True)
        st.download_button(
            f"💾 Download {fft_csv_filename(fd, cs)}",
            export_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=fft_csv_filename(fd, cs),
            mime="text/csv",
        )

    elif mode == "All files / all channels (ZIP)":
        if st.button("Generate ZIP"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                meta_all = []
                for fname in sel_files:
                    fd = files[fname]
                    if fd.sampling_rate is None:
                        continue
                    for cs in get_active_channels(fd, cfg):
                        specs = compute_file_spectrograms(fd, cfg, channels=[cs])
                        if cs.display_name not in specs:
                            continue
                        d = specs[cs.display_name]
                        df_ex = build_fft_export_dataframe(d["spctgram"], d["freq"], d["t"], fd.clock_array, fd.sampling_rate, cfg["exclude_start"])
                        zf.writestr(fft_csv_filename(fd, cs), df_ex.to_csv(index=False))
                        meta_all.append(fft_meta_dict(fd, cs, cfg, d["freq"]))
                zf.writestr("metadata.json", json.dumps(meta_all, ensure_ascii=False, indent=2))
            buf.seek(0)
            st.download_button("💾 Download fft_export.zip", buf.getvalue(), file_name="fft_export.zip", mime="application/zip")

    elif mode == "EEG channel mean per file":
        if st.button("Generate ZIP"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname in sel_files:
                    fd = files[fname]
                    if fd.sampling_rate is None:
                        continue
                    eeg_chs = [cs for cs in get_active_channels(fd, cfg) if cs.channel_type == "EEG" and cs.use_for_eeg_average]
                    specs = compute_file_spectrograms(fd, cfg, channels=eeg_chs)
                    if not specs:
                        continue
                    arrs = [d["spctgram"] for d in specs.values()]
                    minT = min(a.shape[1] for a in arrs)
                    stacked = np.stack([a[:, :minT] for a in arrs], axis=0).mean(axis=0)
                    ref = next(iter(specs.values()))
                    df_ex = build_fft_export_dataframe(stacked, ref["freq"], ref["t"][:minT], fd.clock_array, fd.sampling_rate, cfg["exclude_start"])
                    base = re.sub(r"\.[^.]+$", "", fd.file_name)
                    zf.writestr(f"{safe_filename(base)}_selected_channels_mean_fft.csv", df_ex.to_csv(index=False))
            buf.seek(0)
            st.download_button("💾 Download eeg_mean_fft.zip", buf.getvalue(), file_name="eeg_mean_fft.zip", mime="application/zip")

    elif mode == "Condition mean (by Task)":
        if st.button("Generate ZIP"):
            buf = export_condition_mean(files, cfg, group_keys=("subject", "task"))
            st.download_button("💾 Download condition_mean.zip", buf.getvalue(), file_name="condition_mean.zip", mime="application/zip")

    elif mode == "Condition mean (by Task & Phase)":
        if st.button("Generate ZIP"):
            buf = export_condition_mean(files, cfg, group_keys=("subject", "task", "phase"))
            st.download_button("💾 Download condition_mean_phase.zip", buf.getvalue(), file_name="condition_mean_phase.zip", mime="application/zip")


def export_condition_mean(files: dict[str, FileData], cfg: dict, group_keys: tuple[str, ...]) -> io.BytesIO:
    """条件 (Subject, Task[, Phase]) ごとに EEG 平均 PSD を作って ZIP に詰める。"""
    groups: dict[tuple, list[FileData]] = {}
    for fd in files.values():
        key = tuple(getattr(fd, k) for k in group_keys)
        groups.setdefault(key, []).append(fd)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, fds in groups.items():
            spec_list = []
            ref_freq = None
            ref_t = None
            for fd in fds:
                if fd.sampling_rate is None:
                    continue
                eeg_chs = [cs for cs in get_active_channels(fd, cfg) if cs.channel_type == "EEG" and cs.use_for_eeg_average]
                specs = compute_file_spectrograms(fd, cfg, channels=eeg_chs)
                if not specs:
                    continue
                arrs = [d["spctgram"] for d in specs.values()]
                minT = min(a.shape[1] for a in arrs)
                stacked = np.stack([a[:, :minT] for a in arrs], axis=0).mean(axis=0)
                spec_list.append(stacked)
                ref = next(iter(specs.values()))
                ref_freq = ref["freq"]
                ref_t = ref["t"][:minT]
            if not spec_list:
                continue
            minT = min(a.shape[1] for a in spec_list)
            avg = np.stack([a[:, :minT] for a in spec_list], axis=0).mean(axis=0)
            df_ex = build_fft_export_dataframe(avg, ref_freq, ref_t[:minT], None, fds[0].sampling_rate or 0.0, cfg["exclude_start"])
            name = "_".join(safe_filename(str(k)) for k in key if k)
            zf.writestr(f"{name}_mean_fft.csv", df_ex.to_csv(index=False))
    buf.seek(0)
    return buf


# =====================================================================
# Tab 11: Export Report
# =====================================================================

def tab_export_report(state: dict, cfg: dict) -> None:
    st.header("📦 Export Report")
    files = filter_files(state["files"], cfg)
    if not files:
        st.info("表示するファイルがありません。")
        return

    st.caption("CSV と JSON をまとめた ZIP を生成します。")
    if st.button("Build report ZIP"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # summary_by_file
            rows = []
            for k, fd in state["files"].items():
                rows.append({
                    "file": fd.file_name, "subject": fd.subject, "task": fd.task,
                    "trial": fd.trial, "phase": fd.phase, "memo": fd.memo,
                    "duration": fd.duration, "sampling_rate": fd.sampling_rate,
                    "unit": fd.unit, "channels": len(fd.channel_list), "status": fd.status,
                })
            # 出力 CSV のファイル名はタブ名と一致させる
            zf.writestr("File_Overview.csv", pd.DataFrame(rows).to_csv(index=False))

            if "_band_power_table" in state:
                zf.writestr("Band_Power.csv", state["_band_power_table"].to_csv(index=False))
            if "_condition_comparison" in state:
                zf.writestr("Per-file_Summary.csv", state["_condition_comparison"].to_csv(index=False))
            if "_group_compare" in state:
                zf.writestr("Multi-file_Comparison.csv", state["_group_compare"].to_csv(index=False))
            if "_paired_compare" in state:
                zf.writestr("Group_Comparison.csv", state["_paired_compare"].to_csv(index=False))
            if "_channel_quality" in state:
                zf.writestr("Channel_Quality.csv", state["_channel_quality"].to_csv(index=False))

            # channel settings (per file)
            ch_settings = {fd.file_name: serialize_channel_settings(fd) for fd in state["files"].values() if not fd.is_fft_csv}
            zf.writestr("channel_settings.json", json.dumps(ch_settings, ensure_ascii=False, indent=2))
            zf.writestr("filename_rule_settings.json", json.dumps(state["filename_rule"], ensure_ascii=False, indent=2))
            zf.writestr("analysis_settings.json", json.dumps({k: v for k, v in cfg.items() if not callable(v)}, ensure_ascii=False, indent=2, default=str))

        buf.seek(0)
        st.download_button("💾 Download report.zip", buf.getvalue(), file_name="report.zip", mime="application/zip")

    # 既存FFT CSV 表示
    st.markdown("---")
    st.subheader("Existing FFT CSV viewer (mind_*.csv)")
    fft_files = [k for k, fd in state["files"].items() if fd.is_fft_csv]
    if fft_files:
        sel = st.selectbox("FFT CSV", fft_files, key="rep_fft_sel")
        df = state["files"][sel].fft_csv_dataframe
        if df is not None:
            st.dataframe(df.head(20), use_container_width=True)
            freq_cols = [c for c in df.columns if re.fullmatch(r"F\d+(\.\d+)?", c)]
            if freq_cols:
                freqs = np.array([float(c[1:]) for c in freq_cols])
                vals = df[freq_cols].to_numpy()
                # スペクトログラム
                fig = go.Figure(data=go.Heatmap(z=10 * np.log10(np.maximum(vals.T, 1e-12)), x=df.get("Time", np.arange(len(df))), y=freqs, colorscale="Jet"))
                fig.update_layout(xaxis_title="Time", yaxis_title="Frequency [Hz]", height=400)
                st.plotly_chart(fig, use_container_width=True)
                fig2 = go.Figure(data=go.Scatter(x=freqs, y=vals.mean(axis=0), mode="lines"))
                fig2.update_layout(xaxis_title="Frequency [Hz]", yaxis_title="Mean PSD", height=320, yaxis_type="log")
                st.plotly_chart(fig2, use_container_width=True)
    else:
        st.caption("FFT CSV 形式のファイルをアップロードすると、ここで再表示できます。")


# =====================================================================
# main
# =====================================================================

def render_reset_button(state: dict) -> None:
    """メインエリア右上のリセットボタン。誤クリック防止のため2段階確認。"""
    n_files = len(state.get("files", {}))
    col_title, col_reset = st.columns([5, 2])
    with col_title:
        st.markdown("### 🧠 Polymate Multi-CSV Analyzer")
    with col_reset:
        if n_files == 0:
            return
        with st.popover(f"🗑 Reset ({n_files} files)", use_container_width=True):
            st.warning("アップロード済みのファイル・チャンネル設定・解析結果がすべて削除されます。")
            confirm = st.checkbox("実行することを確認しました", key="reset_confirm")
            if confirm and st.button("⚠️ Reset all", key="reset_btn", type="primary", use_container_width=True):
                state["files"] = {}
                # file_uploader のキーをインクリメントしてサイドバーの表示もクリア
                state["upload_counter"] = state.get("upload_counter", 0) + 1
                for k in list(state.keys()):
                    if k.startswith("_"):
                        del state[k]
                for k in list(st.session_state.keys()):
                    if any(k.startswith(p) for p in (
                        "mc_", "gc2_", "cmp_", "exp_", "bp_", "ba_", "pc_",
                        "ch_target", "preset_", "channel_editor_", "bulk_rename_",
                        "bulk_order_", "prop_", "file_overview_editor",
                        "reset_confirm", "wave_", "fft_", "rep_", "file_uploader_",
                        "sb_",  # サイドバーの multiselect (Subject/Task/Trial/Phase/Channels 等)
                    )):
                        try:
                            del st.session_state[k]
                        except Exception:
                            pass
                st.success("All files cleared.")
                st.rerun()


def main() -> None:
    st.set_page_config(page_title="Polymate Multi-CSV Analyzer", layout="wide")
    state = get_state()

    cfg = render_sidebar(state)

    render_reset_button(state)

    tabs = st.tabs([
        "1. File Overview", "2. Channel Settings", "3. Waveform",
        "4. FFT / Spectrogram", "5. Band Power", "6. Per-file Summary",
        "7. Multi-file Comparison", "8. Group Comparison", "9. Channel Quality",
        "10. FFT CSV Export", "11. Export Report",
    ])
    with tabs[0]:
        tab_overview(state, cfg)
    with tabs[1]:
        tab_channel_settings(state, cfg)
    with tabs[2]:
        tab_waveform(state, cfg)
    with tabs[3]:
        tab_fft(state, cfg)
    with tabs[4]:
        tab_band_power(state, cfg)
    with tabs[5]:
        tab_condition_comparison(state, cfg)
    with tabs[6]:
        tab_group_compare(state, cfg)
    with tabs[7]:
        tab_paired_compare(state, cfg)
    with tabs[8]:
        tab_quality(state, cfg)
    with tabs[9]:
        tab_fft_export(state, cfg)
    with tabs[10]:
        tab_export_report(state, cfg)


if __name__ == "__main__":
    main()
