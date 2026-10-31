import os
import sys
from pathlib import Path

# 실제 모델 로드를 막고 server를 import 한다.
os.environ["SUMEUNJIP_NO_LOAD"] = "1"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
