#!/usr/bin/env python3
"""Compatibility entry point for the packaged, reusable ADB simulator."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adbtv.simulator import main

if __name__ == "__main__":
    raise SystemExit(main())
