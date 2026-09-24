"""Детекция темпа и битов (spectral flux + автокорреляция), без тяжёлых зависимостей."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SR = 22050
HOP = 512
N_FFT = 1024


@dataclass
class BeatInfo:
    bpm: float
    beats: list[float] = field(default_factory=list)  # секунды
    duration: float = 0.0

    def grid(self, start: float = 0.0) -> list[float]:
        return [b - start for b in self.beats if b >= start]


def onset_envelope(y: np.ndarray, sr: int = SR) -> np.ndarray:
    if len(y) < N_FFT:
        return np.zeros(1, dtype=np.float32)
    n_frames = 1 + (len(y) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = y[idx] * np.hanning(N_FFT)[None, :]
    mag = np.log1p(10 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # убираем медленный тренд
    flux = np.maximum(flux, 0)
    return (flux / (flux.max() + 1e-9)).astype(np.float32)


def detect_beats(y: np.ndarray, sr: int = SR, bpm_range: tuple[float, float] = (70.0, 180.0)) -> BeatInfo:
    duration = len(y) / sr
    env = onset_envelope(y, sr)
    fps = sr / HOP
    if env.size < 8 or env.max() <= 0:
        return BeatInfo(bpm=0.0, beats=[], duration=duration)

    # Темп: максимум автокорреляции огибающей в допустимом диапазоне периодов
    ac = np.correlate(env, env, mode="full")[env.size - 1 :]
    lo = int(fps * 60 / bpm_range[1])
    hi = min(int(fps * 60 / bpm_range[0]), ac.size - 1)
    if hi <= lo:
        return BeatInfo(bpm=0.0, beats=[], duration=duration)
    lags = np.arange(lo, hi + 1)
    # Небольшой приоритет темпам около 120 BPM (типичные для коротких видео)
    bpms = 60 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    period = int(lags[np.argmax(ac[lo : hi + 1] * weight)])
    # Уточнение периода параболической интерполяцией
    p = float(period)
    if 1 <= period < ac.size - 1:
        a, b, c = ac[period - 1], ac[period], ac[period + 1]
        denom = a - 2 * b + c
        if denom != 0:
            p = period + 0.5 * (a - c) / denom

    # Фаза: сдвиг, при котором сетка собирает максимум огибающей
    best_phase, best_score = 0, -1.0
    for phase in range(max(1, period)):
        pos = np.round(np.arange(phase, env.size, p)).astype(int)
        pos = pos[pos < env.size]
        score = float(env[pos].sum())
        if score > best_score:
            best_score, best_phase = score, phase
    beats = np.arange(best_phase, env.size, p) * HOP / sr
    # Выравнивание каждого бита по ближайшему локальному пику (±40 мс)
    win = max(1, int(0.04 * fps))
    refined = []
    for b in beats:
        i = int(round(b * fps))
        s, e = max(0, i - win), min(env.size, i + win + 1)
        j = s + int(np.argmax(env[s:e]))
        refined.append((j * HOP + N_FFT / 2) / sr)  # время центра окна
    return BeatInfo(bpm=round(60 * fps / p, 1), beats=[round(x, 3) for x in refined], duration=duration)


def analyze_music(path: str) -> BeatInfo:
    from ..media.ffmpeg import decode_audio_mono

    return detect_beats(decode_audio_mono(path, SR), SR)
