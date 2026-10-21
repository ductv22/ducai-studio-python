"""Isolated resource-recovery probe with real, unchanged Paddle recognition.

Only the reported free-memory budget is constrained for the first requests of
each mode. No memory is allocated as pressure, and no frame/reading is invented.
This fixture never connects to the live job or its database.
"""
import json
import os
import runpy
import sys

base = runpy.run_path(os.environ["DUCAI_OCR_RESOURCE_BASE_HOST"])
runtime_factory = base["_paddle_runtime"]
free_gpu = base["_available_gpu_mb"]
release_after = int(os.environ.get("DUCAI_OCR_RESOURCE_RELEASE_AFTER", "3"))
assert release_after > 0
request_count = 0


def constrained_gpu_budget(device):
    return 0 if request_count < release_after else free_gpu(device)


def resource_runtime(mode, device, batch_size, worker_count=3):
    global request_count
    request_count = 0
    runtime = runtime_factory(mode, device, batch_size, worker_count)
    recognize = runtime.recognize_batched

    def counted(frames, bands):
        global request_count
        request_count += 1
        if request_count == release_after:
            print(json.dumps({"diagnostic_resource_constraint_released": mode,
                              "request": request_count}), file=sys.stderr, flush=True)
        return recognize(frames, bands)

    runtime.recognize_batched = counted
    return runtime


runtime_factory.__globals__["_available_gpu_mb"] = constrained_gpu_budget
globals().update({key: value for key, value in base.items() if not key.startswith("__")})
_paddle_runtime = resource_runtime
