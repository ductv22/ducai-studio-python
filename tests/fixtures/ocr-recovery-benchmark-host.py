"""Isolated real-model probe: interrupt one owned predictor, never fabricate OCR.

Loaded by ocr-trace-host.py. The archived baseline or current host is selected
explicitly; the only fault is terminating a child created by this coordinator.
No live database, GUI process, or unrelated GPU task is controlled here.
"""
import json
import os
import runpy
import sys
import threading

base = runpy.run_path(os.environ["DUCAI_OCR_RECOVERY_BASE_HOST"])
child_type = base["ChildPredictor"]
terminate = base["_terminate_owned_tree"]
fault_lock = threading.Lock()
request_count = 0
fault_injected = False
fail_after = int(os.environ.get("DUCAI_OCR_RECOVERY_FAIL_AFTER", "18"))


class InterruptedChild(child_type):
    def recognize(self, frames, bands):
        global request_count, fault_injected
        with fault_lock:
            request_count += 1
            interrupt = not fault_injected and request_count >= fail_after
            if interrupt:
                fault_injected = True
        if interrupt:
            terminate(self.process, force=True)
            self.process.wait(timeout=5)
            print(json.dumps({"diagnostic_owned_predictor_interrupted": self.process.pid}),
                  file=sys.stderr, flush=True)
        return super().recognize(frames, bands)


# Only the coordinator's constructor changes. New predictors execute the real
# base host directly, with its unchanged Paddle model/settings and crop inputs.
base["ParallelPaddleRuntime"].__init__.__globals__["ChildPredictor"] = InterruptedChild
globals().update({key: value for key, value in base.items() if not key.startswith("__")})
