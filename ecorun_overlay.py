#!/usr/bin/env python3
"""後方互換のためのラッパ。実体は ecorun パッケージ(ecorun/cli.py)。

  python ecorun_overlay.py LOG0650.CSV --preview 150 -o preview.png
  uv run ecorun-overlay LOG0650.CSV --preview 150 -o preview.png   # 同じ
"""
from ecorun.cli import main

if __name__ == "__main__":
    main()
