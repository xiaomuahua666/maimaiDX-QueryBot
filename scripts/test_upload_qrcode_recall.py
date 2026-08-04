"""maiu 二维码撤回不得阻塞上传流程的回归检查。"""

from pathlib import Path


source = (Path(__file__).parents[1] / "command" / "mai_account.py").read_text()

helper_start = source.index("async def _recall_qrcode_message")
helper_end = source.index("\n\ndef ", helper_start)
helper_source = source[helper_start:helper_end]

assert "recall_message" in helper_source
assert "foreign_recall_notice" in helper_source
assert "_QRCODE_RECALL_TIMEOUT_SECONDS" in helper_source

upload_start = source.index("@upload_fish.handle()")
upload_end = source.index("\n\n@account_ping.handle()", upload_start)
upload_source = source[upload_start:upload_end]

assert upload_source.count("await _recall_qrcode_message(bot, event)") == 2
assert "await bot.delete_msg(message_id=event.message_id)" not in upload_source
assert upload_source.count('recall_notice = ""') == 2

# QR credentials must be recalled before reactions/network work, and resumed
# ``got`` matchers must not rely on exact runtime type equality.
for marker in ('@upload_fish.handle()', '@upload_fish.got("upload_qrcode")'):
    handler = upload_source[upload_source.index(marker):]
    handler = handler[:handler.index("\n\n@", 1) if "\n\n@" in handler[1:] else len(handler)]
    assert handler.index("await _recall_qrcode_message(bot, event)") < handler.index(
        "await react_processing(bot, event)"
    )
assert "type(matcher) is upload_" not in source
assert "isinstance(matcher, upload_fish)" in source
assert "matcher.state[_UPLOAD_MODE_STATE_KEY]" in source

print("upload qrcode recall timeout tests: ok")
