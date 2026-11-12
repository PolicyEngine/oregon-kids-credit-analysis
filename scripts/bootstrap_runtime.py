#!/usr/bin/env python3
"""Authenticate upstream artifacts and prepare an isolated development runtime."""

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
WRAPPER_VERSION = "6.17.2"
WRAPPER_WHEEL_SHA = "d86ba67f7093453fbb3249275c1802ed9dbadb06582fddff447f181958468088"
WRAPPER_SDIST_SHA = "ba372b9edf2686cd8fd3c6cb2d09a8dc61225ba7e22726746bb2a1e40cac8c9e"
SPM_VERSION = "1.1.0"
SPM_WHEEL_SHA = "c528ec0f06eeb1188b7d6cf456ff3a9b73a0fd8856b3938358251cc5f9be83e8"
SPM_FORECAST_SHA = "335e6755dc8e0bd883e09b76c2bd53a5315683773e44d50085c7d6b8832b6291"
CORE_VERSION = "3.34.2"
REGIONAL_WRAPPER_COMMIT = "7147b459e320753acf8ac2674215b780da3dbd6d"
MANAGED_SCOPE_WRAPPER_COMMIT = "628cbaf63e31dce8477be4b0215c07bcc3b49d47"
MANAGED_SCOPE_WRAPPER_VERSION = "6.18.0"
MANAGED_SCOPE_WHEEL_SHA = (
    "8cc4287740ea38ac7fb1527bea52d1f1bb9128aafdfe0d6b821897ab95a37c3f"
)
MANAGED_SCOPE_SDIST_SHA = (
    "728e1907378536ebf229c28d18ac24db04d74349ddf8dbec4bb79349cbd74388"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(package: str, version: str, suffix: str, expected: str | None = None):
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.load(response)
    candidates = [row for row in payload["urls"] if row["filename"].endswith(suffix)]
    if len(candidates) != 1 or candidates[0]["yanked"]:
        raise ValueError(f"Expected one non-yanked {package} {suffix}")
    row = candidates[0]
    digest = row["digests"]["sha256"]
    if expected is not None and digest != expected:
        raise ValueError(f"Registry hash changed for pinned {package} artifact")
    destination = RUNTIME / "artifacts" / row["filename"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or sha256(destination) != digest:
        with urllib.request.urlopen(row["url"], timeout=120) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"Downloaded {package} artifact failed SHA verification")
        destination.write_bytes(content)
    return destination, {"url": row["url"], "sha256": digest, "version": version}


def verify_model_source(wheel: Path, source: Path, commit: str):
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != commit:
        raise ValueError(f"Model HEAD {actual} differs from requested {commit}")
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--", "policyengine_us"],
        text=True,
    )
    if dirty:
        raise ValueError("Country package source has uncommitted changes")
    checked = 0
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if not member.startswith("policyengine_us/") or member.endswith("/"):
                continue
            local = source / member
            if not local.is_file() or local.read_bytes() != archive.read(member):
                raise ValueError(f"Country wheel differs from source: {member}")
            checked += 1
    if not checked:
        raise ValueError("Country wheel contains no country-package files")
    return checked


def verify_wrapper_source(wheel: Path, source: Path, commit: str):
    """Authenticate the explicitly selected, publicly reviewed adapter build."""
    if commit not in (REGIONAL_WRAPPER_COMMIT, MANAGED_SCOPE_WRAPPER_COMMIT):
        raise ValueError("Unrecognized regional wrapper source commit")
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != commit:
        raise ValueError("Wrapper HEAD differs from requested public commit")
    remote = subprocess.check_output(
        ["git", "-C", str(source), "remote", "get-url", "origin"], text=True
    ).strip()
    if remote != "https://github.com/PolicyEngine/policyengine.py.git":
        raise ValueError("Wrapper source must use the canonical upstream remote")
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--", "src/policyengine"],
        text=True,
    )
    if dirty:
        raise ValueError("Wrapper package source has uncommitted changes")
    checked = 0
    tracked = subprocess.check_output(
        [
            "git",
            "-C",
            str(source),
            "ls-tree",
            "-r",
            "--name-only",
            commit,
            "src/policyengine",
        ],
        text=True,
    ).splitlines()
    expected_members = {name.removeprefix("src/") for name in tracked}
    with zipfile.ZipFile(wheel) as archive:
        members = {
            name
            for name in archive.namelist()
            if name.startswith("policyengine/") and not name.endswith("/")
        }
        if members != expected_members:
            raise ValueError("Wrapper wheel package inventory differs from source")
        for member in archive.namelist():
            if not member.startswith("policyengine/") or member.endswith("/"):
                continue
            local = source / "src" / member
            if not local.is_file() or local.read_bytes() != archive.read(member):
                raise ValueError(f"Wrapper wheel differs from source: {member}")
            checked += 1
    if not checked:
        raise ValueError("Wrapper wheel contains no package files")
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--role", choices=("baseline", "candidate"))
    parser.add_argument("--model-wheel", type=Path)
    parser.add_argument("--model-source", type=Path)
    parser.add_argument("--model-commit")
    parser.add_argument("--wrapper-wheel", type=Path)
    parser.add_argument("--wrapper-source", type=Path)
    parser.add_argument("--wrapper-commit")
    parser.add_argument("--runtime-prefix", choices=("regional-ids", "managed-scope"))
    args = parser.parse_args()
    scope_profile = args.runtime_prefix == "managed-scope"
    reference_version = (
        MANAGED_SCOPE_WRAPPER_VERSION if scope_profile else WRAPPER_VERSION
    )
    wrapper_wheel, wrapper_info = artifact(
        "policyengine",
        reference_version,
        ".whl",
        MANAGED_SCOPE_WHEEL_SHA if scope_profile else WRAPPER_WHEEL_SHA,
    )
    source_archive, source_info = artifact(
        "policyengine",
        reference_version,
        ".tar.gz",
        MANAGED_SCOPE_SDIST_SHA if scope_profile else WRAPPER_SDIST_SHA,
    )
    core_wheel, core_info = artifact("policyengine-core", CORE_VERSION, ".whl")
    spm_wheel, spm_info = artifact("spm-calculator", SPM_VERSION, ".whl", SPM_WHEEL_SHA)
    override = (args.wrapper_wheel, args.wrapper_source, args.wrapper_commit)
    if any(override) or args.runtime_prefix:
        if (
            not all(override)
            or args.runtime_prefix not in ("regional-ids", "managed-scope")
            or args.prepare_only
        ):
            parser.error(
                "Source wrapper builds require all wrapper arguments "
                "and an explicit --runtime-prefix"
            )
        expected_commit = (
            MANAGED_SCOPE_WRAPPER_COMMIT if scope_profile else REGIONAL_WRAPPER_COMMIT
        )
        if args.wrapper_commit != expected_commit:
            parser.error("Wrapper source commit differs from its runtime profile")
        wrapper_wheel = args.wrapper_wheel.resolve(strict=True)
        wrapper_files = verify_wrapper_source(
            wrapper_wheel, args.wrapper_source.resolve(), args.wrapper_commit
        )
        wrapper_info = {
            "version": reference_version,
            "source_commit": args.wrapper_commit,
            "source_repository": "https://github.com/PolicyEngine/policyengine.py",
            "sha256": sha256(wrapper_wheel),
            "build_status": "unreleased_source_build",
            "package_files_verified": wrapper_files,
        }
    source = RUNTIME / f"policyengine-{reference_version}"
    if not source.exists():
        with tarfile.open(fileobj=io.BytesIO(source_archive.read_bytes())) as archive:
            archive.extractall(RUNTIME, filter="data")
    # Authenticate the two helper files on every invocation, including a reused tree.
    with tarfile.open(source_archive) as archive:
        for relative in (
            "tests/fixtures/spm_development.py",
            "scripts/spm_bundle.py",
            "scripts/bundle.py",
        ):
            packaged = archive.extractfile(
                f"policyengine-{reference_version}/{relative}"
            )
            if packaged is None or (source / relative).read_bytes() != packaged.read():
                raise ValueError(f"Upstream development helper changed: {relative}")
    RUNTIME.mkdir(exist_ok=True)
    identities = {
        "wrapper": wrapper_info,
        "wrapper_source_archive": source_info,
        "spm_calculator": spm_info,
        "core_version": CORE_VERSION,
        "core": core_info,
    }
    suffix = f"-{args.runtime_prefix}" if args.runtime_prefix else ""
    (RUNTIME / f"upstream-artifacts{suffix}.json").write_text(
        json.dumps(identities, indent=2)
    )
    if args.prepare_only:
        print(json.dumps({"source": str(source), "artifacts": identities}, indent=2))
        return
    if not all((args.role, args.model_wheel, args.model_source, args.model_commit)):
        parser.error(
            "Installation needs role, model wheel, model source and model commit"
        )
    wheel = args.model_wheel.resolve(strict=True)
    checked = verify_model_source(wheel, args.model_source.resolve(), args.model_commit)
    name = f"{args.runtime_prefix}-{args.role}" if args.runtime_prefix else args.role
    env = RUNTIME / f"env-{name}"
    python = env / "bin/python"
    if not python.exists():
        subprocess.run(["uv", "venv", "--python", "3.13", str(env)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(wrapper_wheel),
            str(core_wheel),
            str(spm_wheel),
            str(wheel),
            "pytest==9.2.0",
        ],
        check=True,
    )
    manifest = RUNTIME / f"development-{name}.json"
    subprocess.run(
        [
            str(python),
            "scripts/bundle.py",
            "development-manifest",
            "--country-wheel",
            str(wheel),
            "--calculator-wheel",
            str(spm_wheel),
            "--output",
            str(manifest),
        ],
        cwd=source,
        env={**__import__("os").environ, "POLICYENGINE_SKIP_COUNTRY_IMPORTS": "1"},
        check=True,
    )
    # Authenticate installed Core and calculator bytes before deriving any
    # runtime settings. The upstream fixture repeats the calculator check.
    verification = """
import importlib.metadata as m, sys, zipfile
from pathlib import Path
for package, module, wheel in zip(
    ('policyengine-core', 'spm-calculator', 'policyengine'),
    ('policyengine_core', 'spm_calculator', 'policyengine'), sys.argv[1:]
):
    with zipfile.ZipFile(wheel) as z:
        d = m.distribution(package)
        for name in z.namelist():
            if name.startswith(module + '/') and not name.endswith('/'):
                p = Path(d.locate_file(name))
                if not p.is_file() or p.read_bytes() != z.read(name):
                    raise ValueError('Installation differs from wheel: ' + name)
from spm_calculator import load_forecast
print(load_forecast().content_sha256)
"""
    forecast_sha = subprocess.check_output(
        [
            str(python),
            "-c",
            verification,
            str(core_wheel),
            str(spm_wheel),
            str(wrapper_wheel),
        ],
        text=True,
    ).strip()
    if forecast_sha != SPM_FORECAST_SHA:
        raise ValueError("The authenticated SPM forecast differs from its recorded pin")
    # This fixture explicitly denies certification. Record the real Core wheel
    # and forecast used by this development runtime; inherited release pins are
    # references, not compatibility claims.
    payload = json.loads(manifest.read_text())
    payload["packages"]["policyengine-core"].update(
        version=CORE_VERSION,
        sha256=core_info["sha256"],
        wheel_url=core_wheel.resolve().as_uri(),
        install_requirement=f"policyengine-core=={CORE_VERSION}",
    )
    payload["development"]["core_runtime_override"] = core_info
    payload["development"]["forecast_runtime_override"] = {
        "forecast_content_sha256": forecast_sha,
        "calculator_wheel_sha256": spm_info["sha256"],
    }
    manifest.write_text(json.dumps(payload, indent=2) + "\n")
    # Use the unchanged upstream configuration function and validators. Its CLI
    # writes the packaged bundle; this call writes only our development fixture.
    configure = """
import json, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from spm_bundle import configure_spm
path = Path(sys.argv[1])
payload = json.loads(path.read_text())
settings = dict(payload['measurements']['spm'], forecast_content_sha256=sys.argv[2])
updated = configure_spm(payload, settings, None)
path.write_text(json.dumps(updated, indent=2) + '\\n')
"""
    subprocess.run(
        [str(python), "-c", configure, str(manifest), forecast_sha],
        cwd=source,
        env={**__import__("os").environ, "POLICYENGINE_SKIP_COUNTRY_IMPORTS": "1"},
        check=True,
    )
    identity = {
        **identities,
        "model_source_commit": args.model_commit,
        "model_wheel_sha256": sha256(wheel),
        "model_package_files_verified": checked,
        "model_wheel": str(wheel),
        "wrapper_wheel": str(wrapper_wheel),
        "source": str(source),
        "development_manifest": str(manifest),
        "python": str(python),
        "forecast_content_sha256": forecast_sha,
    }
    (RUNTIME / f"runtime-{name}.json").write_text(json.dumps(identity, indent=2))
    freeze = subprocess.check_output(
        ["uv", "pip", "freeze", "--python", str(python)], text=True
    )
    (RUNTIME / f"lock-{name}.txt").write_text(freeze)
    print(json.dumps(identity, indent=2))


if __name__ == "__main__":
    main()
