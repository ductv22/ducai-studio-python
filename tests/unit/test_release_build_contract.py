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


def test_release_uses_upstream_vieneu_cuda_without_legacy_monkey_patch() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )

    assert "vieneu_v330_optimization.py" not in build_script
    assert '"whisper_engine_host"' in build_script


def test_public_update_assets_are_signed_and_source_free() -> None:
    release_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )
    update_script = (PROJECT_ROOT / "scripts" / "build-update-assets.py").read_text(
        encoding="utf-8"
    )
    wrapper = (PROJECT_ROOT / "scripts" / "build-update-assets.ps1").read_text(
        encoding="utf-8"
    )

    assert '"ocr_engine_host"' in release_script
    assert '"tts_engine_host"' in release_script
    assert '"$SourceName.pyc"' in release_script
    assert "dfile='engine_hosts/'+sys.argv[3]+'.py'" in release_script
    assert "DUCAI_BUILD_VERSION" in release_script
    assert "DUCAI_BUILD_VERSION" in update_script
    source_copy = (
        "Copy-Item -LiteralPath (Join-Path $projectRoot "
        '"src\\ducai_studio\\engine_hosts'
    )
    assert source_copy not in release_script
    service_source = (PROJECT_ROOT / "src/ducai_studio/job_manager/service.py").read_text(
        encoding="utf-8"
    )
    assert service_source.index("if compiled_path.is_file()") < service_source.index(
        "return packaged_path.resolve(strict=True)"
    )
    assert 'path.suffix.casefold() in {".py", ".whl", ".pem", ".key"}' in update_script
    assert "private_key.sign(encoded)" in update_script
    assert "public_bytes != expected_public_key" in update_script
    assert 'EndsWith("-UPDATE")' in wrapper


def test_release_always_bundles_the_licensed_scene_text_font() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )

    assert '"src\\ducai_studio\\ui\\assets\\fonts"' in build_script
    assert '"PlaywriteVN-Variable.ttf"' in build_script
    assert 'throw "Playwrite VN scene-text font is missing."' in build_script
    assert '"Agbalumo-Regular.ttf"' in build_script
    assert 'throw "Agbalumo ink-calligraphy font is missing."' in build_script
    fonts_root = PROJECT_ROOT / "src" / "ducai_studio" / "ui" / "assets" / "fonts"
    assert (fonts_root / "DancingScript-Variable.ttf").is_file()
    assert (fonts_root / "Lobster-Regular.ttf").is_file()
    assert (fonts_root / "Agbalumo-Regular.ttf").is_file()
    for font_name in (
        "ComforterBrush-Regular.ttf",
        "WaterBrush-Regular.ttf",
        "Splash-Regular.ttf",
        "Hurricane-Regular.ttf",
    ):
        assert (fonts_root / font_name).is_file()
        assert f'"{font_name}"' in build_script
    notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Dancing Script" in notices
    assert "Lobster" in notices
    assert "Agbalumo" in notices
    assert "Comforter Brush" in notices
    assert "Water Brush" in notices
    assert "Splash" in notices
    assert "Hurricane" in notices
    assert "[IO.Path]::GetRelativePath" not in build_script


def test_release_refreshes_runtime_qml_even_when_reusing_compiled_deployment() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )
    assert '$qmlSource = Join-Path $projectRoot "src\\ducai_studio\\ui\\qml"' in build_script
    assert '$qmlTarget = Join-Path $releaseRoot "ui\\qml"' in build_script
    assert 'Copy-Item -Path (Join-Path $qmlSource "*")' in build_script
    assert build_script.index('$qmlTarget =') < build_script.index('$files = Get-ChildItem')


def test_release_labels_and_packages_the_exact_freshly_compiled_executable() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )

    fresh_path = "$executablePath = $freshExecutable.FullName"
    identity_write = (
        "Set-Content -LiteralPath $deploymentIdentity -Value $version -Encoding ascii"
    )
    selected_executable = "$executable = Get-Item -LiteralPath $executablePath"
    package_selected = (
        'Copy-Item -LiteralPath $executable.FullName -Destination '
        '(Join-Path $releaseRoot "DucaiStudio2.exe") -Force'
    )

    assert "$buildStartedUtc = [DateTime]::UtcNow" in build_script
    assert "Where-Object { $_.LastWriteTimeUtc -ge $buildStartedUtc }" in build_script
    assert fresh_path in build_script
    assert build_script.index(fresh_path) < build_script.index(identity_write)
    assert selected_executable in build_script
    assert package_selected in build_script
    assert build_script.index(selected_executable) < build_script.index(package_selected)
    assert (
        'Move-Item -LiteralPath $generatedMain -Destination '
        '(Join-Path $releaseRoot "DucaiStudio2.exe")'
    ) not in build_script


def test_artifact_checker_uses_owner_local_wheel_for_source_parity() -> None:
    checker = (
        PROJECT_ROOT / "tests" / "fixtures" / "release-artifact-check.py"
    ).read_text(encoding="utf-8")

    assert 'parser.add_argument("--validation-wheel", type=Path)' in checker
    assert '(root / "dist").glob(f"ducai_studio-{__version__}-*.whl")' in checker
    assert 'release.glob("ducai_studio-*.whl")' not in checker
    assert "The validation wheel version must match the customer release." in checker
    assert "The validation wheel must remain outside the customer release." in checker
    assert 'root / "tests/fixtures/license-bootstrap"' in checker
    assert '"DUCAI_TEST_LICENSE_BOOTSTRAP": "1"' in checker


def test_portable_manifest_hashes_bundled_engine_runtimes() -> None:
    build_script = (
        PROJECT_ROOT / "scripts" / "build-portable-release.ps1"
    ).read_text(encoding="utf-8")

    for critical_root in (
        '"engine_hosts"',
        '"paddle_runtime"',
        '"piper_runtime"',
        '"vieneu_3_8_runtime"',
        '"python_runtime_311"',
        '"python_runtime_312"',
    ):
        assert critical_root in build_script


def test_portable_whisper_runtime_and_model_are_explicitly_opt_in() -> None:
    build_script = (
        PROJECT_ROOT / "scripts" / "build-portable-release.ps1"
    ).read_text(encoding="utf-8")

    assert "[switch]$IncludeWhisper" in build_script
    assert "if ($IncludeWhisper)" in build_script
    assert 'whisper_included = [bool]$IncludeWhisper' in build_script
    assert '"models--Systran--faster-whisper-large-v3"' in build_script
