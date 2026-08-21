"""二维码动态预计耗时回归测试（无需启动 NoneBot）。"""

import importlib.util
import sys
import tempfile
import types
from pathlib import Path


root = Path(__file__).resolve().parent.parent
path = root / "libraries" / "maimaidx_processing_time.py"
package_name = "processing_time_test_package"
package = types.ModuleType(package_name)
package.__path__ = [str(path.parent)]
sys.modules[package_name] = package
spec = importlib.util.spec_from_file_location(
    f"{package_name}.maimaidx_processing_time", path
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

assert module.auto_qrcode_workflow_key(pc=True, fish=True, lxns=False) == (
    "auto_qrcode:pc+fish"
)
assert module.auto_qrcode_fallback_seconds(pc=True, fish=True, lxns=True) == 71
assert "首次预计约 71 秒" in module.format_processing_estimate(71, 0)
assert module.upload_workflow_key(fish=True, lxns=False) == "explicit_upload:fish"
assert module.upload_workflow_key(fish=False, lxns=True) == "explicit_upload:lxns"
assert module.upload_workflow_key(fish=True, lxns=True) == "explicit_upload:all"
assert module.upload_fallback_seconds(fish=True, lxns=True) == 70

with tempfile.TemporaryDirectory() as td:
    estimator = module.ProcessingTimeEstimator(
        Path(td) / "timing.db", sample_limit=3
    )
    assert estimator.estimate("flow", fallback_seconds=40) == (40, 0)
    estimator.record("flow", 10)
    estimator.record("flow", 20)
    estimator.record("flow", 30)
    estimator.record("flow", 40)
    # sample_limit=3，仅保留 20/30/40，平均 30。
    assert estimator.estimate("flow", fallback_seconds=40) == (30, 3)

playcount_source = (root / "command" / "mai_playcount.py").read_text(
    encoding="utf-8"
)
assert "processing_time_estimator.estimate(" not in playcount_source
assert "processing_time_estimator.record(" in playcount_source
assert "Bot 无法撤回原凭据消息，请立即手动撤回" in playcount_source

account_source = (root / "command" / "mai_account.py").read_text(encoding="utf-8")
assert "📤 已受理，正在上传到" in account_source
assert "processing_time_estimator.estimate(" in account_source
assert "upload_fallback_seconds(" in account_source
assert "processing_time_estimator.record(" in account_source
assert "上游服务未返回错误详情" in account_source

analysis_source = (root / "command" / "mai_b50_analysis.py").read_text(
    encoding="utf-8"
)
assert '_ANALYSIS_TIMING_KEY = "b50_analysis"' in analysis_source
assert "processing_time_estimator.estimate" in analysis_source
assert "_format_analysis_estimate(estimated, samples)" in analysis_source
assert "成功锐评的真实平均耗时" in analysis_source
assert "processing_time_estimator.record" in analysis_source
assert "本次锐评用时" in analysis_source

assert "_recall_sensitive_qrcode_message" in playcount_source
assert "recall_message" in playcount_source
assert "Bot 无法撤回原凭据消息，请立即手动撤回" in playcount_source

print("processing time estimator tests: ok")
