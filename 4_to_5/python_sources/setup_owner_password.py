"""Choose a local owner password; no personal verifier is distributed."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'local_app'))
from cafe_start import configure
if __name__=='__main__':
    raise SystemExit(configure())
