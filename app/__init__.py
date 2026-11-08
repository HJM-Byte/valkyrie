"""瓦尔基里 · 视觉语音项目 —— 应用包。

约定：仓库根目录放代码与 ``.venv``；模型目录用 ``ROBOT_MODELS`` 指定，
默认 ``<repo>/models``。运行用仓库自己的 ``.venv\\Scripts\\python.exe``。
"""
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
