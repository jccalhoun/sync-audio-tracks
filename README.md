# sync-audio-tracks

`micsync.py` lines up several microphones that recorded the **same voice at the same time**, so the tracks can be blended without sounding hollow or phasey.

It was built for a setup where several USB mics (including wireless lapel mics) record into one computer at once, for example in Adobe Audition. Even then the tracks don't stay lined up:

- **Each USB device has its own clock.** The tracks start a little apart and then slowly drift further apart, typically a few milliseconds over a 10-minute take. Shifting a track by a single fixed offset can't fix that, which is why the sync features in most audio editors fall short here.
- **Mics in different places hear you at slightly different times.** When you move, especially relative to a lapel mic that moves with you, the offset between mics changes by a fraction of a millisecond.

Blended mics start to sound hollow with well under 1 ms of error, so `micsync.py` measures the offset to the sample every second through the take and resamples each track so it stays locked to a reference mic from start to finish.

## Windows app (no Python needed)

Download `micsync-gui.exe` from the [Releases page](https://github.com/jccalhoun/sync-audio-tracks/releases) and double-click it. (The newest build is also under [Actions](https://github.com/jccalhoun/sync-audio-tracks/actions): open the latest successful run and download `micsync-windows` at the bottom of the page. That needs a GitHub login.) It is the same window described below, with Python and every library packed inside, including drag and drop. `micsync.exe` is the command-line version.

- The app takes a few seconds to appear, because it unpacks itself each time it starts.
- It isn't code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time. Choose **More info**, then **Run anyway**. Some antivirus programs are also suspicious of apps packaged this way.

Both programs are built and tested automatically on GitHub from the code in this repository (see `.github/workflows/build-exe.yml`).

## Requirements

*Only needed to run the Python scripts. The Windows app needs nothing installed.*

- Python 3.9 or newer
- numpy, scipy and soundfile:

```
python -m pip install numpy scipy soundfile
```

On Windows, if the install fails with a permissions error such as `[WinError 2] ... .deleteme`, install into your user folder instead:

```
python -m pip install --user numpy scipy soundfile
```

## Using the window

Double-click `micsync_gui.pyw` (keep it in the same folder as `micsync.py`), or run `python micsync_gui.pyw`.

1. **Add the recordings of one take.** Use *Add files...* or *Add folder...*, or drag the files or the folder onto the window. Adding a folder skips files already ending in `-synced`.
2. **Choose the reference.** The first file is the reference to start with. To change it, double-click another file, or select it and press *Use as reference*.
3. **Choose where to save.** Either next to the original files, or in a folder you pick.
4. **Press Sync.** The bar shows progress, the report appears below, and *Open output folder* takes you to the results. *Cancel* stops after the current step; tracks already finished are kept.

The two checkboxes match the command-line options: unticking *Follow movement between mics* is `--steady`, and unticking *Correct inverted polarity* is `--no-flip`.

Drag and drop needs one extra library. Without it the window works the same, using the buttons:

```
python -m pip install tkinterdnd2
```

## Command-line usage

**Simplest: put one take's recordings in a folder with the script and run it with no file names.**

```
python micsync.py
```

It syncs every WAV file in the folder. The first file by name is the reference (`Track 1_001.wav` comes before `Track 2_001.wav`, and `Track 2` before `Track 10`). Files already ending in `-synced` are ignored, so you can run it again safely. You can also point it at another folder:

```
python micsync.py "C:\recordings\take 3"
```

Keep one take per folder. Files from a different take can't be matched to the reference, so they would be skipped after wasting some time.

**To choose the reference or the files yourself,** list the reference mic first, then the others:

```
python micsync.py "Track 4_001.wav" "Track 1_001.wav" "Track 2_001.wav"
```

Put quotes around file names that contain spaces. Audition's default names do. Wildcards such as `"Track ?_001.wav"` also work, including on Windows. A file you name explicitly is always used, even one ending in `-synced`; only folder mode and wildcards skip those.

While it runs, a status line shows what it's doing for each track (reading, measuring, then resampling with a percentage). Expect roughly 30 seconds per track for an 8-minute take.

The reference is the mic every other track is matched to. Any mic works; the one you use most is a natural choice. All files must have the same sample rate.

### Options

| Option | Default | What it does |
|---|---|---|
| `--out FOLDER` | next to the originals | Write the synced files and the report to this folder. It is created if missing. |
| `--steady` | off | Correct clock drift only. Don't follow offset changes caused by movement. |
| `--no-flip` | off | Report an inverted-polarity mic, but don't flip it. |
| `--interval SECONDS` | 1 | Time between measurements (0.1 to 60). |
| `--max-offset SECONDS` | 1 | Largest starting offset to search for (0.02 to 60). Raise it if a mic starts more than a second away from the reference. |
| `--suffix TEXT` | `synced` | Added to the output file names. |

## Output

Your original files are never changed. Alongside them you get:

- **One `-synced.wav` file per non-reference mic,** for example `Track 2_001-synced.wav`. Each has the same sample rate, bit depth and channel count as its original, and is exactly the same length as the reference. Place the reference and the synced files at 0:00 in a multitrack session and they stay aligned to the end.
- **A report,** for example `Track 1_001-sync-report.txt`, also printed to the screen.

Use your original file for the reference mic; it isn't rewritten.

## Reading the report

```
Track 2_001.wav
  start offset : +39.66 ms (+1903.8 samples)
  end offset   : +45.26 ms (+2172.3 samples)
  clock drift  : +6.06 ms over the take (+11.66 ppm)
  movement     : offset wanders +/-5.3 samples around the drift line
  polarity     : normal
  correction   : clock drift + following your movement
  measurements : 321 of 385 windows used, scatter 2.5 samples
  NOTE: 6:45.5-8:01.4 offset shifted -47 samples (-0.98 ms) from the drift line (followed)
```

| Line | Meaning |
|---|---|
| start / end offset | How far behind (+) or ahead (−) of the reference this mic was at the start and end. Wireless mics usually have tens of milliseconds of built-in delay. |
| clock drift | How far the two clocks slid apart over the take. Mics sharing one USB device or receiver show the same drift. |
| movement | Typical offset change from you moving, on top of the drift. |
| polarity | `INVERTED` means this mic's signal was upside down relative to the reference. It's flipped back unless you use `--no-flip`. |
| measurements | How many one-second windows gave a usable match, and how consistent they were. Silent stretches aren't counted. |
| NOTE | A stretch where the offset sat noticeably away from the drift line, by up to 3 ms. This is usually you leaning or turning: about 1 ms is roughly 34 cm (a foot) of change in distance between two mics. If you didn't move, listen for a click where it starts, which would mean the device dropped a few samples. |
| WARNING | One of three things. Fewer than half the windows matched: check that track by ear. The offset stepped by more than 3 ms and stayed there: that's too large to be movement, so the device probably dropped or repeated audio; listen where the stretch starts and ends. Or samples clipped when writing a 16- or 24-bit file, which can't happen with 32-bit float files. |
| SKIPPED | No synced file was written for this track. The line says why: it couldn't be matched to the reference (for example, a file from a different take), its sample rate differs, or the file is unreadable or too short. The other tracks still finish. |

## How it works

1. **Measure.** A band-limited mono copy of each track is used for measuring only. A rough pass finds the starting offset and the polarity; a precise pass then cross-correlates one-second windows against the reference, every second, with sub-sample accuracy.
2. **Fit.** A straight line through the measurements gives the clock drift. What's left over is movement, which is followed with a short running median. Large jumps are followed only if they hold steady for several seconds, so noises during quiet stretches can't throw off the correction.
3. **Resample.** Each track is resampled along that offset curve with a high-quality windowed-sinc filter, so every sample lands where the reference expects it. The audio is otherwise untouched.

## Limitations

- **Two mics never match perfectly.** Different mics, positions and room reflections mean even a perfect sync won't cancel completely. The aim is a blend that sounds full, not identical tracks.
- **Aligning removes the natural time difference between mic positions.** That's usually what you want when blending close mics on one voice, but it slightly changes the sound compared with the raw tracks.
- **It needs speech in the reference track** to measure from. Long silences are bridged using the drift line.
- **Clicks from dropped samples stay.** If a USB device drops audio, the script keeps the alignment correct afterwards but can't remove the click itself.
- **All tracks must share a sample rate.** A track with a different rate is skipped with a message.

## License

MIT. See [LICENSE](LICENSE).
