#!/usr/bin/env python3
"""Build the three extensions: python3 tools/build_oxt.py [outdir]  ->  sk_libre-<app>-ai.oxt"""
import sys, zipfile
from pathlib import Path
root = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1] if len(sys.argv) > 1 else root / "dist")
out.mkdir(parents=True, exist_ok=True)
for app in ("writer", "calc", "math"):
    src = root / app
    target = out / f"sk_libre-{app}-ai.oxt"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(src).as_posix())
    print("built", target)
