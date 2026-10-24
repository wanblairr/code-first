"""Portable entry point for the included Python workbench."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'local_app'))
from cafe_start import main
if __name__=='__main__':
    raise SystemExit(main())
