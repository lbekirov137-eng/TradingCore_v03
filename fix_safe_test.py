from pathlib import Path
import shutil

path = Path(r"tests/test_paper_live_loop.py")
backup = Path(r"tests/test_paper_live_loop.before_safe_stop.py")

shutil.copy2(path, backup)

text = path.read_text(encoding="utf-8")

start = text.index(
    "def test_pipeline_trade_opens_position"
)

end = text.find(
    "\ndef ",
    start + 1,
)

if end == -1:
    end = len(text)

block = text[start:end]

if "class SafeTradeEngine" not in block:
    marker = "    manager = build_manager(tmp_path)\n"

    safe_engine = '''
    class SafeTradeEngine(TradeEngine):
        def execute(self, context):
            result = super().execute(context)
            result.execution["paper_order"]["stop"] = 96.0
            return result

'''

    assert marker in block, "MANAGER LINE NOT FOUND"

    block = block.replace(
        marker,
        marker + safe_engine,
        1,
    )

block = block.replace(
    "engine=TradeEngine(),",
    "engine=SafeTradeEngine(),",
    1,
)

assert "engine=SafeTradeEngine()," in block

path.write_text(
    text[:start] + block + text[end:],
    encoding="utf-8",
)

print("SAFE TEST PATCH: OK")
print("TEST STOP: 96.0")
print("PRODUCTION LIMITS: UNCHANGED")
