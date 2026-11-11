#!/usr/bin/env python3
"""
micsync - lock several recordings of the same voice to one reference mic.

Built for USB mics recorded together on one computer: each mic runs on its
own clock, so the tracks start slightly apart AND slowly drift apart; and
because the mics sit in different places, the offset also shifts a little
whenever you move. This measures the offset to the sample every second
through the take, then resamples each track so it stays locked to the
reference from start to end.

Usage:
    python micsync.py REFERENCE.wav MIC2.wav MIC3.wav MIC4.wav

Output (originals are never changed):
    MIC2-synced.wav ...            same length, sample rate and format as REFERENCE
    REFERENCE-sync-report.txt      offsets, drift, polarity, warnings

Needs:  pip install numpy scipy soundfile
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

WIN_S = 1.0        # length of each analysis window (seconds); short so drift inside it is negligible
LOCK_S = 4.0       # longer window used only to find the initial offset
NEAR_S = 0.010     # normal search range around the predicted offset
RETRY_S = 0.250    # wider search if the normal one fails (first pass only)
MIN_CONF = 0.30    # minimum correlation (0..1) to accept a first-pass window
MIN_CONF2 = 0.50   # minimum correlation to use a second-pass window
TOL = 8.0          # samples: further than this from the steady-drift line counts as "shifted"
RUN = 5            # report a shift only if it lasts at least this many windows
MOVE_S = 0.001     # offset changes up to this (seconds) are always followed; larger ones only if steady
FOLLOW = 2         # follow the offset using the median of +/- this many neighbouring windows
TAPS = 64          # resampling filter length
PHASES = 512       # resampling filter sub-sample resolution
CHUNK = 100_000    # samples resampled per batch (memory vs speed)


def status(msg=""):
    """Show progress on one line that keeps overwriting itself; status() clears it.
    Does nothing when the output is not a terminal (e.g. redirected to a file)."""
    if sys.stdout.isatty():
        sys.stdout.write("\r" + msg.ljust(50) + ("" if msg else "\r"))
        sys.stdout.flush()


# ---------------------------------------------------------------- measuring

def analysis_signal(x, sr):
    """Mono, band-limited copy used only for measuring (never written out)."""
    mono = x.mean(axis=1)
    sos = signal.butter(4, [200, min(8000, sr * 0.45)], btype="band", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, mono).astype(np.float32)


def segment(x, start, length):
    """x[start:start+length], zero-padded where it runs off either end."""
    out = np.zeros(length)
    lo, hi = max(start, 0), min(start + length, len(x))
    if hi > lo:
        out[lo - start:hi - start] = x[lo:hi]
    return out


def measure(ref_a, trk_a, start, win, d_pred, margin, pol=0):
    """
    Find d such that trk[n + d] matches ref[n] for the window at `start`,
    searching d_pred +/- margin. pol=0 accepts a peak of either sign (used to
    discover polarity); pol=+1/-1 only looks for peaks of that sign.
    Returns (t, d, conf, at_edge): t is where in the reference the measurement
    applies (energy centre of the window), d is fractional, conf is the signed
    correlation (-1..1; negative = track is polarity-inverted).
    """
    r = segment(ref_a, start, win)
    s = segment(trk_a, start + d_pred - margin, win + 2 * margin)
    c = signal.correlate(s, r, mode="valid", method="fft")
    score = np.abs(c) if pol == 0 else pol * c
    j = int(np.argmax(score))
    seg = s[j:j + win]
    conf = c[j] / (np.sqrt(np.dot(r, r) * np.dot(seg, seg)) + 1e-20)
    at_edge = j == 0 or j == len(c) - 1
    frac = 0.0
    if not at_edge:  # parabolic fit for sub-sample precision
        y0, y1, y2 = score[j - 1:j + 2]
        den = y0 - 2 * y1 + y2
        if den != 0:
            frac = 0.5 * (y0 - y2) / den
    e = r * r
    t = start + float(np.dot(e, np.arange(win)) / (e.sum() + 1e-20))
    return t, d_pred + j - margin + frac, conf, at_edge


def first_pass(ref_a, trk_a, sr, interval, max_offset):
    """Rough offsets through the take, polarity unknown. Returns (starts, t, d, conf)."""
    win, lock_win = int(WIN_S * sr), int(LOCK_S * sr)
    gate = 0.1 * np.sqrt(np.mean(ref_a.astype(np.float64) ** 2))
    starts, out, recent, misses = [], [], [], 0
    for start in range(0, max(len(ref_a) - win, 1), int(interval * sr)):
        r = ref_a[start:start + win]
        if np.sqrt(np.mean(r.astype(np.float64) ** 2)) < gate:
            continue  # reference is silent here, nothing to match
        starts.append(start)
        if not recent:  # no idea yet: long window, wide search
            _, d0, c0, edge = measure(ref_a, trk_a, start, lock_win, 0, int(max_offset * sr))
            if abs(c0) < MIN_CONF or edge:
                continue
            recent = [d0]
        d_pred = int(round(np.median(recent)))
        for margin in (int(NEAR_S * sr), int(RETRY_S * sr)):
            t, d, conf, edge = measure(ref_a, trk_a, start, win, d_pred, margin)
            if abs(conf) >= MIN_CONF and not edge:
                break
        else:
            misses += 1
            if misses >= 5:  # lost it: search from scratch next time
                recent, misses = [], 0
            continue
        misses = 0
        recent = (recent + [d])[-5:]
        out.append((t, d, conf))
    t, d, conf = (np.array(v) for v in zip(*out)) if out else (np.array([]),) * 3
    return starts, t, d, conf


def second_pass(ref_a, trk_a, sr, starts, predict, pol):
    """Precise offsets: known polarity, narrow search around the first-pass trend."""
    win, near = int(WIN_S * sr), int(NEAR_S * sr)
    out = []
    for start in starts:
        d_pred = int(round(float(predict(start + win / 2))))
        t, d, conf, edge = measure(ref_a, trk_a, start, win, d_pred, near, pol)
        if conf * pol >= MIN_CONF2 and not edge:
            out.append((t, d))
    return (np.array(v) for v in zip(*out)) if out else (np.array([]), np.array([]))


# ------------------------------------------------------------------ fitting

def runmed(x, half):
    """Running median over +/- `half` neighbours (fewer at the ends).
    Ignores short runs of bad values but keeps real steps."""
    x = np.asarray(x, dtype=float)
    return np.array([np.median(x[max(0, i - half):i + half + 1]) for i in range(len(x))])


def runs(mask, min_len):
    """(first, last) index of each run of True at least min_len long."""
    out, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    return out


def fit(t, d, n_out, sr, steady):
    """Turn the measurements into an offset curve d(n). Returns a dict."""
    if len(t) < 5:
        c0 = float(np.median(d))
        return dict(slope=0.0, start=c0, end=c0, scatter=0.0, move=0.0, shifts=[], glitches=[],
                    used=len(t), mode="fixed", curve=lambda n: c0 + 0.0 * n)
    # steady clock drift: a straight line through the windows that agree with it
    lag = max(1, min(20, len(t) // 3))
    slope = float(np.median((d[lag:] - d[:-lag]) / (t[lag:] - t[:-lag])))
    icpt = float(np.median(d - slope * t))
    for _ in range(4):
        inl = np.abs(d - (slope * t + icpt)) <= TOL
        if inl.sum() < 5:
            break
        slope, icpt = np.polyfit(t[inl], d[inl], 1)
    # What is left over, window by window:
    #   small  -> you moving relative to the mics: follow it
    #   large but steady for a while -> dropped/repeated audio: follow it and warn
    #   large and erratic -> not a real match (noises away from the mics): ignore
    res = d - (slope * t + icpt)
    bound = MOVE_S * sr
    keep = np.abs(res) <= bound
    glitches = []
    for a, b in runs(~keep, RUN):
        med = np.median(res[a:b + 1])
        steady_run = np.abs(res[a:b + 1] - med) <= TOL
        if steady_run.mean() >= 0.7:
            keep[a:b + 1] = steady_run
            glitches.append((float(t[a]), float(t[b]), float(med)))
    if not keep.any():  # nothing lines up consistently: report no match instead of guessing
        return None
    tk, rk = t[keep], res[keep]
    local = runmed(rk, FOLLOW)
    shifts = [(float(tk[a]), float(tk[b]), float(np.median(local[a:b + 1])))
              for a, b in runs(np.abs(local) > TOL, RUN)]
    small = np.abs(local) <= TOL
    move = float(np.percentile(np.abs(local[small]), 95)) if small.any() else 0.0
    if steady:
        mode, curve = "steady", (lambda n: icpt + slope * n)
        scatter = float(np.std(rk[np.abs(rk) <= TOL])) if (np.abs(rk) <= TOL).any() else 0.0
    else:
        at = np.concatenate([[0.0], tk, [float(n_out)]])
        ad = np.concatenate([[local[0]], local, [local[-1]]])
        mode, curve = "follow", (lambda n: icpt + slope * n + np.interp(n, at, ad))
        scatter = float(np.std((rk - local)[np.abs(rk - local) <= TOL]))
    return dict(slope=float(slope), start=float(curve(0.0)), end=float(curve(float(n_out))), scatter=scatter,
                move=move, shifts=shifts, glitches=glitches, used=int(keep.sum()), mode=mode, curve=curve)


# --------------------------------------------------------------- resampling

def build_table():
    """Windowed-sinc interpolation filters for PHASES sub-sample positions."""
    half = TAPS // 2
    k = np.arange(TAPS) - (half - 1)
    arg = k[None, :] - (np.arange(PHASES + 1) / PHASES)[:, None]
    w = np.i0(10.0 * np.sqrt(np.clip(1 - (arg / half) ** 2, 0, None))) / np.i0(10.0)
    h = np.sinc(arg) * w
    return h / h.sum(axis=1, keepdims=True), k


def warp(x, curve, n_out, table, k, progress=None):
    """out[n] = x[n + d(n)], band-limited interpolation, per channel."""
    half = TAPS // 2
    n_in = len(x)
    xp = np.pad(x, ((half, half), (0, 0)))
    out = np.zeros((n_out, x.shape[1]))
    for n0 in range(0, n_out, CHUNK):
        if progress:
            progress(n0 / n_out)
        n = np.arange(n0, min(n0 + CHUNK, n_out), dtype=np.float64)
        p = n + curve(n)
        i = np.floor(p).astype(np.int64)
        f = p - i
        valid = (i >= 0) & (i <= n_in - 1)
        idx = np.clip(i, 0, n_in - 1)[:, None] + k[None, :] + half
        ph = f * PHASES
        lo = np.minimum(ph.astype(np.int64), PHASES - 1)
        wgt = (ph - lo)[:, None]
        h = table[lo] * (1 - wgt) + table[lo + 1] * wgt
        for c in range(x.shape[1]):
            y = np.einsum("nt,nt->n", xp[idx, c], h)
            y[~valid] = 0.0
            out[n0:n0 + len(n), c] = y
    return out


# --------------------------------------------------------------------- main

def mmss(samples, sr):
    s = samples / sr
    return f"{int(s // 60)}:{s % 60:04.1f}"


def main():
    ap = argparse.ArgumentParser(description="Sync multiple mics of the same voice to a reference mic.")
    ap.add_argument("reference", type=Path, help="the mic everything else is matched to")
    ap.add_argument("tracks", type=Path, nargs="+", help="the other mic files")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between measurements (default 1)")
    ap.add_argument("--max-offset", type=float, default=1.0, help="largest start offset to search for, seconds (default 1)")
    ap.add_argument("--suffix", default="synced", help="added to output file names (default: synced)")
    ap.add_argument("--steady", action="store_true", help="correct clock drift only; do not follow offset changes caused by movement")
    ap.add_argument("--no-flip", action="store_true", help="report inverted polarity but do not correct it")
    args = ap.parse_args()
    missing = [p for p in [args.reference, *args.tracks] if not p.is_file()]
    if missing:
        sys.exit("File not found: " + ", ".join(str(p) for p in missing)
                 + "\nCheck the exact names (e.g. 'dir *.wav'), and put quotes around names with spaces.")

    info = sf.info(str(args.reference))
    sr = info.samplerate
    status("Reading reference...")
    ref, _ = sf.read(str(args.reference), always_2d=True, dtype="float64")
    n_out = len(ref)
    ref_a = analysis_signal(ref, sr)
    del ref
    table, k = build_table()
    status()

    lines = [f"Reference: {args.reference.name}  ({sr} Hz, {info.subtype}, {mmss(n_out, sr)})", ""]
    print(lines[0])
    failed = 0
    for path in args.tracks:
        t0 = time.time()
        print(f"\n{path.name}")
        tinfo = sf.info(str(path))
        if tinfo.samplerate != sr:
            msg = f"  SKIPPED: sample rate is {tinfo.samplerate} Hz but the reference is {sr} Hz."
            print(msg); lines += [path.name, msg, ""]; failed += 1
            continue
        status("  reading...")
        x, _ = sf.read(str(path), always_2d=True, dtype="float64")
        trk_a = analysis_signal(x, sr)
        status("  measuring...")
        starts, t1, d1, c1 = first_pass(ref_a, trk_a, sr, args.interval, args.max_offset)
        voiced, m = len(starts), None
        if len(t1):
            pol = -1 if np.sum(c1) < 0 else 1
            same = np.sign(c1) == pol
            rough = runmed(d1[same], 4)
            t, d = second_pass(ref_a, trk_a, sr, starts, lambda n: np.interp(n, t1[same], rough), pol)
            if len(t):
                m = fit(t, d, n_out, sr, args.steady)
        if m is None:
            status()
            msg = "  SKIPPED: could not find a reliable match with the reference."
            print(msg); lines += [path.name, msg, ""]; failed += 1
            continue

        y = warp(x, m["curve"], n_out, table, k, progress=lambda done: status(f"  resampling... {done:.0%}"))
        flipped = pol < 0 and not args.no_flip
        if flipped:
            y = -y
        out_path = path.with_name(f"{path.stem}-{args.suffix}{path.suffix}")
        status("  writing...")
        sf.write(str(out_path), y, sr, subtype=tinfo.subtype, format=tinfo.format)
        status()

        ms = lambda s: s / sr * 1000
        how = {"follow": "clock drift + following your movement", "steady": "clock drift only (--steady)",
               "fixed": "fixed offset (too little speech to measure drift)"}
        rep = [
            f"  start offset : {ms(m['start']):+.2f} ms ({m['start']:+.1f} samples)",
            f"  end offset   : {ms(m['end']):+.2f} ms ({m['end']:+.1f} samples)",
            f"  clock drift  : {ms(m['slope'] * n_out):+.2f} ms over the take ({m['slope'] * 1e6:+.2f} ppm)",
            f"  movement     : offset wanders +/-{m['move']:.1f} samples around the drift line",
            f"  polarity     : " + ("normal" if pol > 0 else "INVERTED" + (" (corrected)" if flipped else " (left as is)")),
            f"  correction   : {how[m['mode']]}",
            f"  measurements : {m['used']} of {voiced} windows used, scatter {m['scatter']:.1f} samples",
        ]
        for a, b, v in m["shifts"]:
            rep.append(f"  NOTE: {mmss(a, sr)}-{mmss(b, sr)} offset shifted {v:+.0f} samples ({ms(v):+.2f} ms) from the drift line"
                       + (" (not followed with --steady)" if m["mode"] == "steady" else " (followed)"))
        if m["shifts"]:
            rep.append("        Usually you moving relative to the mics. If you didn't move, check for a click where it starts.")
        if m["used"] < voiced // 2:
            rep.append("  WARNING: fewer than half the windows matched - check this track by ear")
        rep.append(f"  wrote        : {out_path.name}  ({time.time() - t0:.0f} s)")
        print("\n".join(rep))
        lines += [path.name] + rep + [""]

    report = args.reference.with_name(f"{args.reference.stem}-sync-report.txt")
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {report.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
