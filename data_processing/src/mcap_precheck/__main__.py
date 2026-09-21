"""`python -m mcap_precheck` / `python src/mcap_precheck` 入口。"""

import sys
from pathlib import Path

# src 布局：导入根是 src（不是 apis）。这里补进 sys.path，
# 使 `python src/mcap_precheck ...` 在任意 cwd 下都能直接跑。
_SRC = str(Path(__file__).resolve().parents[1])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mcap_precheck.cli import main  # noqa: E402

raise SystemExit(main())
