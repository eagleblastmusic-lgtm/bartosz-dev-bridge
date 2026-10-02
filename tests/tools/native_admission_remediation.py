"""Isolated framed Native bridge for the actual Browser worker retention test."""
from dataclasses import fields
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bdb_vnext.m9b_native_host import read_native_message, write_native_message
from bdb_vnext.m3a_submission import ShadowSubmissionRequest, ShadowSubmissionStore
from bdb_vnext.m3b_browser_admission import AdmissionEnvelope, ShadowNativeAdmissionBridge, M3B_PROTOCOL_GENERATION

root = Path(sys.argv[1])
store = ShadowSubmissionStore(root / "control", shadow=True, legacy_root=root / "legacy")
while message := read_native_message(sys.stdin.buffer):
    response = {"schema": "bdb-vnext-native-response-v1", "generation_id": "bdb-vnext-g1", "protocol_generation": "bdb-vnext-protocol-v1",
                "native_host_name": "com.bartosz.dev_bridge.vnext", "browser_extension_id": "mopnolkjddkmgojfjkenjobehhmmklll", "status": "success"}
    try:
        bridge = ShadowNativeAdmissionBridge(store)
        if message["action"] == "admission.submit":
            data = message["request"]
            request = ShadowSubmissionRequest(**{field.name: data[field.name] for field in fields(ShadowSubmissionRequest) if field.name in data})
            response["receipt"] = bridge.send(AdmissionEnvelope(request)).as_dict()
        elif message["action"] == "admission.lookup":
            receipt = bridge.lookup(message["submission_key"], message["request_digest"], protocol_generation=M3B_PROTOCOL_GENERATION)
            response["receipt"] = receipt.as_dict() if receipt else None
        elif message["action"] == "test.restart":
            store.close()
            store = ShadowSubmissionStore(root / "control", shadow=True, legacy_root=root / "legacy")
        elif message["action"] == "test.stats": response["counts"] = store.counts()
        else: raise ValueError("Unsupported fixture action")
    except Exception as exc:
        response.update(status="failed", error_code=getattr(exc, "code", "fixture_failed"))
    write_native_message(sys.stdout.buffer, response)
store.close()
