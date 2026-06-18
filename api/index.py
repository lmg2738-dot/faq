import os
import sys

# 프로젝트 루트의 app.py(Flask)를 Vercel Python 런타임에 노출
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from app import app  # noqa: E402  (Flask WSGI application)
