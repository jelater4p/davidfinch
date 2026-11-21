"""Docker sağlık kontrolü: kalp atışı dosyası yakın zamanda güncellendi mi?"""
import os
import sys
import time

MAX_AGE_SECONDS = 30 * 60   # ay başı öğrenmesi birkaç dakika sürebilir; pay bırakıyoruz

db = os.environ.get("DAVIDFINCH_DB", "data/davidfinch.db")
path = os.path.join(os.path.dirname(os.path.abspath(db)), "heartbeat")
ok = os.path.exists(path) and time.time() - os.path.getmtime(path) < MAX_AGE_SECONDS
sys.exit(0 if ok else 1)
