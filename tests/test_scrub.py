"""开源脱敏门禁：全仓文本文件零命中（口径与 tools/check_scrub.py 完全一致）。"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_scrub  # noqa: E402


def test_repo_scrubbed():
    hits = check_scrub.scan(ROOT)
    assert hits == [], "\n".join(hits)
