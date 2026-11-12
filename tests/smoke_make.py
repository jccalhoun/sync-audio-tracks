"""Make two short test recordings: a reference, and a copy that starts 1234 samples late."""
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
sr, seconds, delay = 48000, 20, 1234
n = sr * seconds
rng = np.random.default_rng(1)
bursts = np.clip(np.sin(2 * np.pi * 0.7 * np.arange(n) / sr), 0, None)  # noise bursts with gaps, like speech
x = (rng.normal(0, 0.1, n) * bursts).astype("float32")
sf.write(str(out / "ref.wav"), x, sr, subtype="FLOAT")
sf.write(str(out / "mic 2.wav"), np.concatenate([np.zeros(delay, "float32"), x])[:n], sr, subtype="FLOAT")
print("wrote test recordings to", out)
