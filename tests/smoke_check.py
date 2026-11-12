"""Check the result of syncing the recordings from smoke_make.py."""
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

src, out = Path(sys.argv[1]), Path(sys.argv[2])
report = (out / "ref-sync-report.txt").read_text(encoding="utf-8")
print(report)
found = re.search(r"start offset : \S+ ms \(([+-][\d.]+) samples\)", report)
assert found, "no start offset in the report"
offset = float(found.group(1))
assert abs(offset - 1234) < 1, f"expected an offset of 1234 samples, got {offset}"

ref, sr = sf.read(str(src / "ref.wav"))
synced, sr2 = sf.read(str(out / "mic 2-synced.wav"))
assert sr == sr2 and len(ref) == len(synced), "synced file has the wrong length or sample rate"
body = slice(sr, len(ref) - sr)
residual = 10 * np.log10(np.mean((synced[body] - ref[body]) ** 2) / np.mean(ref[body] ** 2))
assert residual < -30, f"synced track does not cancel against the reference ({residual:.1f} dB)"
print(f"OK: offset {offset:+.1f} samples, residual {residual:.1f} dB")
