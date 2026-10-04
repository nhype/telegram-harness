#!/usr/bin/env python3
"""Synthesize the trailer's music bed: a dark D-minor drone that follows the story's intensity.

Deterministic (no randomness without a seed), license-free, 60 s, 48 kHz stereo.
Run: python3 gen_bed.py   ->   bed.wav (convert with ffmpeg to bed.m4a)
"""
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, lfilter, sosfilt

SR = 48000
DUR = 60.0
HERE = Path(__file__).resolve().parent
t = np.arange(int(SR * DUR)) / SR

# Story intensity, (time, level) breakpoints, linear in between.
CURVE = [(0, 0.0), (2.5, 0.30), (9.6, 0.38), (15.0, 0.50), (15.4, 0.12), (17.9, 0.20), (18.0, 0.62),
         (20.0, 0.55), (36.5, 0.62), (37.0, 0.32), (43.0, 0.40), (48.4, 0.85), (48.6, 1.0),
         (54.0, 0.80), (58.0, 0.35), (60.0, 0.0)]


def envelope(points):
    xs, ys = zip(*points)
    return np.interp(t, xs, ys)


def voice(freq, detune=0.15, harmonics=6):
    """A soft saw-like tone: a few decaying harmonics, two detuned copies."""
    out = np.zeros_like(t)
    for cents in (-detune, detune):
        f = freq * 2 ** (cents / 12)
        for h in range(1, harmonics + 1):
            out += np.sin(2 * np.pi * f * h * t + h) / h ** 1.6
    return out


def lowpass_sweep(x, intensity):
    """Brighter as intensity rises: blend a dark and a bright lowpass by the curve."""
    dark = sosfilt(butter(2, 220, fs=SR, output="sos"), x)
    bright = sosfilt(butter(2, 1400, fs=SR, output="sos"), x)
    return dark * (1 - intensity) + bright * intensity


def boom(at, length=3.0, gain=1.0):
    """Sub impact: a pitch drop from 90 Hz to 32 Hz with an exponential tail."""
    out = np.zeros_like(t)
    i0 = int(at * SR)
    n = min(int(length * SR), len(t) - i0)
    tt = np.arange(n) / SR
    freq = 32 + 58 * np.exp(-tt * 9)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    out[i0:i0 + n] = np.sin(phase) * np.exp(-tt * 1.7) * gain
    return out


def pulse(start, end, bpm=100, gain=0.35):
    """Soft heartbeat thumps for the demo section."""
    out = np.zeros_like(t)
    step = 60 / bpm
    k = start
    while k < end:
        out += boom(k, length=0.5, gain=gain) * (1 if int((k - start) / step) % 2 == 0 else 0.6)
        k += step
    return out


def reverb(x, mix=0.35):
    """Small Schroeder reverb: four combs and two allpasses."""
    wet = np.zeros_like(x)
    for delay_ms, fb in ((29.7, 0.78), (37.1, 0.76), (41.1, 0.74), (43.7, 0.72)):
        d = int(SR * delay_ms / 1000)
        a = np.zeros(d + 1)
        a[0], a[d] = 1, -fb
        wet += lfilter([1], a, x)
    for delay_ms, g in ((5.0, 0.7), (1.7, 0.7)):
        d = int(SR * delay_ms / 1000)
        b = np.zeros(d + 1)
        b[0], b[d] = -g, 1
        a = np.zeros(d + 1)
        a[0], a[d] = 1, -g
        wet = lfilter(b, a, wet)
    return x * (1 - mix) + wet * mix / 4


def main():
    intensity = envelope(CURVE)
    lfo = 0.85 + 0.15 * np.sin(2 * np.pi * 0.07 * t)
    chord = voice(73.42) * 0.9 + voice(110.0) * 0.6 + voice(146.83) * 0.45 + voice(174.61) * 0.35
    # the lift for the finale: add A3 and D4 as intensity passes 0.7
    lift = (voice(220.0) * 0.3 + voice(293.66) * 0.22) * np.clip((intensity - 0.7) / 0.3, 0, 1)
    pad = lowpass_sweep(chord + lift, intensity) * intensity * lfo
    sub = boom(18.0, gain=1.1) + boom(48.6, length=4.0, gain=1.3) + pulse(20.4, 36.4)
    left = reverb(pad * 0.9 + sub * 0.8)
    right = reverb(np.roll(pad, 240) * 0.9 + sub * 0.8)
    mix = np.stack([left, right], axis=1)
    mix /= np.max(np.abs(mix)) / 0.89
    wavfile.write(HERE / "bed.wav", SR, (mix * 32767).astype(np.int16))
    print("wrote bed.wav", f"{DUR:.0f}s")


if __name__ == "__main__":
    main()
