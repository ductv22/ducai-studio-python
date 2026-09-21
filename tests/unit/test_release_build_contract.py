from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_windows_release_is_built_without_a_console_window() -> None:
    deploy_spec = (
        PROJECT_ROOT / "src" / "ducai_studio" / "pysidedeploy.spec"
    ).read_text(encoding="utf-8")

    assert "--windows-console-mode=disable" in deploy_spec


def test_default_release_root_is_the_shared_outputs_directory() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )

    assert "..\\..\\outputs" in build_script
    assert 'Join-Path $projectRoot "release"' not in build_script


def test_release_packages_vieneu_cuda_optimization_beside_engine_host() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )

    assert "vieneu_v330_optimization.py" in build_script


def test_portable_manifest_hashes_bundled_engine_runtimes() -> None:
    build_script = (
        PROJECT_ROOT / "scripts" / "build-portable-release.ps1"
    ).read_text(encoding="utf-8")

    for critical_root in (
        '"engine_hosts"',
        '"paddle_runtime"',
        '"piper_runtime"',
        '"vieneu_3_3_runtime"',
        '"python_runtime_311"',
        '"python_runtime_312"',
    ):
        assert critical_root in build_script
