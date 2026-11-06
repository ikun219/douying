"""
Header/Params 构造器快捷入口。

用法:
    from api._builder import HeaderBuilder, HeaderType, Params
    h = HeaderBuilder().build(HeaderType.GET).get()
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DOUYIN = _HERE.parent
if str(_DOUYIN) not in sys.path:
    sys.path.insert(0, str(_DOUYIN))

from builder.header import HeaderBuilder, HeaderType
from builder.params import Params

__all__ = ["HeaderBuilder", "HeaderType", "Params"]