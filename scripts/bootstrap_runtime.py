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
CORE_VERSION = "3.34.2"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--role", choices=("baseline", "candidate"))
    parser.add_argument("--model-wheel", type=Path)
    parser.add_argument("--model-source", type=Path)
    parser.add_argument("--model-commit")
    args = parser.parse_args()
    wrapper_wheel, wrapper_info = artifact(
        "policyengine", WRAPPER_VERSION, ".whl", WRAPPER_WHEEL_SHA
    )
    source_archive, source_info = artifact(
        "policyengine", WRAPPER_VERSION, ".tar.gz", WRAPPER_SDIST_SHA
    )
    core_wheel, core_info = artifact("policyengine-core", CORE_VERSION, ".whl")
    spm_wheel, spm_info = artifact("spm-calculator", SPM_VERSION, ".whl", SPM_WHEEL_SHA)
    source = RUNTIME / f"policyengine-{WRAPPER_VERSION}"
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
            packaged = archive.extractfile(f"policyengine-{WRAPPER_VERSION}/{relative}")
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
    (RUNTIME / "upstream-artifacts.json").write_text(json.dumps(identities, indent=2))
    if args.prepare_only:
        print(json.dumps({"source": str(source), "artifacts": identities}, indent=2))
        return
    if not all((args.role, args.model_wheel, args.model_source, args.model_commit)):
        parser.error(
            "Installation needs role, model wheel, model source and model commit"
        )
    wheel = args.model_wheel.resolve(strict=True)
    checked = verify_model_source(wheel, args.model_source.resolve(), args.model_commit)
    env = RUNTIME / f"env-{args.role}"
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
    manifest = RUNTIME / f"development-{args.role}.json"
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
    # This fixture explicitly denies certification. Record the real Core wheel
    # used by country-model qualification, rather than inherit the release pin.
    payload = json.loads(manifest.read_text())
    payload["packages"]["policyengine-core"].update(
        version=CORE_VERSION,
        sha256=core_info["sha256"],
        wheel_url=core_wheel.resolve().as_uri(),
        install_requirement=f"policyengine-core=={CORE_VERSION}",
    )
    payload["development"]["core_runtime_override"] = core_info
    manifest.write_text(json.dumps(payload, indent=2) + "\n")
    # The upstream fixture authenticates Core's origin/version; authenticate its
    # installed package bytes here too, using the official registry wheel.
    verification = """
import importlib.metadata as m, sys, zipfile
from pathlib import Path
with zipfile.ZipFile(sys.argv[1]) as z:
    d = m.distribution('policyengine-core')
    for name in z.namelist():
        if name.startswith('policyengine_core/') and not name.endswith('/'):
            p = Path(d.locate_file(name))
            if not p.is_file() or p.read_bytes() != z.read(name):
                raise ValueError('Core installation differs from wheel: ' + name)
"""
    subprocess.run([str(python), "-c", verification, str(core_wheel)], check=True)
    identity = {
        **identities,
        "model_source_commit": args.model_commit,
        "model_wheel_sha256": sha256(wheel),
        "model_package_files_verified": checked,
        "model_wheel": str(wheel),
        "source": str(source),
        "development_manifest": str(manifest),
        "python": str(python),
    }
    (RUNTIME / f"runtime-{args.role}.json").write_text(json.dumps(identity, indent=2))
    freeze = subprocess.check_output(
        ["uv", "pip", "freeze", "--python", str(python)], text=True
    )
    (RUNTIME / f"lock-{args.role}.txt").write_text(freeze)
    print(json.dumps(identity, indent=2))


if __name__ == "__main__":
    main()
