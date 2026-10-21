"""Pytest subprocess bootstrap; never shipped or imported by application code."""
from __future__ import annotations

import base64
import importlib.abc
import os
import sys
from importlib.machinery import PathFinder
from pathlib import Path

if os.environ.get("DUCAI_TEST_LICENSE_BOOTSTRAP") == "1":
    from ducai_studio.infrastructure import licensing_state

    _original_init = licensing_state.LicensingState.__init__
    _path = Path(os.environ["DUCAI_TEST_LICENSE_PATH"])
    _device_id = os.environ["DUCAI_TEST_LICENSE_DEVICE_ID"]
    _key_id = os.environ["DUCAI_TEST_LICENSE_KEY_ID"]
    _public_key = base64.urlsafe_b64decode(os.environ["DUCAI_TEST_LICENSE_PUBLIC_KEY"])

    def _test_init(
        self,
        *,
        path=None,
        identity=None,
        public_keys=None,
    ) -> None:
        if path is None and identity is None and public_keys is None:
            _original_init(
                self,
                path=_path,
                identity=lambda: _device_id,
                public_keys={_key_id: _public_key},
            )
            return
        _original_init(self, path=path, identity=identity, public_keys=public_keys)

    licensing_state.LicensingState.__init__ = _test_init

    # UI fixtures construct their model before a Job Manager ping. Seed only
    # their initial test state from the same signed token verified above. Keep
    # this lazy: eagerly importing the UI also imports worker launch modules and
    # changes otherwise unrelated subprocess stderr/protocol behavior.
    _UI_MODULES = {
        "ducai_studio.ui.viewmodels.application_view_model",
        "ducai_studio.ui.viewmodels.license_view_model",
    }

    def _patch_ui_module(module) -> None:
        if module.__name__.endswith("application_view_model"):
            original = module.JobRuntime.__init__

            def licensed_runtime_init(self, *args, **kwargs) -> None:
                original(self, *args, **kwargs)
                self._licensed = licensing_state.LicensingState().status()["active"] is True

            module.JobRuntime.__init__ = licensed_runtime_init
        else:
            original = module.LicenseViewModel.__init__

            def licensed_view_model_init(self, *args, **kwargs) -> None:
                original(self, *args, **kwargs)
                self._status = licensing_state.LicensingState().status()

            module.LicenseViewModel.__init__ = licensed_view_model_init

    class _UiLicenseLoader(importlib.abc.Loader):
        def __init__(self, loader) -> None:
            self._loader = loader

        def create_module(self, spec):
            create = getattr(self._loader, "create_module", None)
            return create(spec) if create is not None else None

        def exec_module(self, module) -> None:
            self._loader.exec_module(module)
            _patch_ui_module(module)

    class _UiLicenseFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in _UI_MODULES:
                return None
            spec = PathFinder.find_spec(fullname, path)
            if spec is not None and spec.loader is not None:
                spec.loader = _UiLicenseLoader(spec.loader)
            return spec

    sys.meta_path.insert(0, _UiLicenseFinder())
