"""Build already-patched fork sources. Never apply the historical lock's patches.

The receipt identifies an implementation, not qualified output. Consumers must
still run fresh server packaged, paired-consumer and retained-case qualification.
Only the binary, two JSON receipts and collected third-party notices enter the
public archive. No fonts, assets, fixtures or runtime archive are bundled.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import uuid

HERE = Path(__file__).resolve().parent
PLATFORM = "linux/amd64"
REPOSITORY = "TaptByHatch/vectorcraft"
FILES = ("vectorcraft-cli", "exporter-build.json", "build-metrics.json", "THIRD-PARTY-NOTICES.txt")
MAX_NOTICES = 32 * 1024 * 1024


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def read_pin():
    pin = (HERE / "libs-revision.txt").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise ValueError("libs-revision.txt must contain one full patched fork commit")
    return pin


def verify_sources(lock, paths, commit, libs_commit):
    """Check fork revisions, upstream ancestry and every patched file before Docker."""
    for source in lock["sources"]:
        path = paths[source["name"]]
        expected = commit if source["name"] == "vectorcraft" else libs_commit
        if git(path, "rev-parse", "HEAD") != expected:
            raise ValueError("fork revision mismatch: " + source["name"])
        git(path, "merge-base", "--is-ancestor", source["commit"], "HEAD")
        for name, digest in source["patched_files"].items():
            file = path / name
            if file.is_symlink() or not file.is_file() or sha(file) != digest:
                raise ValueError("patched file hash mismatch: " + source["name"] + "/" + name)


def build_identity(lock, recipe, paths):
    # Keep exactly the worker exporter/build.py identity, including historical
    # upstream revisions and patch hashes, but excluding local cache directory names.
    return dict(
        platform=PLATFORM,
        builder_image=recipe["amd64_builder_image"],
        recipe=recipe,
        sources=[{key: value for key, value in source.items() if key != "cache"}
                 for source in lock["sources"]],
        cargo_locks={name: sha(path / "Cargo.lock") for name, path in paths.items()},
        output_settings=lock["output_settings"],
    )


def test_count(log):
    return sum(map(int, re.findall(r"test result: ok\. (\d+) passed", log)))


def write_notices(lock, paths, cargo, output):
    """Collect license text, never assets, with portable source-relative labels."""
    sections = [
        "Tapt exporter modifications\n"
        "Tapt applies normalized continuous process-color components, preserves "
        "process color spaces, and corrects text spacing in the exporter.\n"
        + "".join(f"Upstream {source['name']}: {source['repository']} at {source['commit']}\n"
                  for source in lock["sources"])
    ]
    total = len(sections[0].encode("utf-8"))
    roots = [(name, path, True) for name, path in paths.items()]
    for relative in ("registry/src", "git/checkouts"):
        root = cargo / relative
        if not any(path.is_symlink() for path in (cargo, root, root.parent)):
            roots.append(("cargo/" + relative, root, False))
    excluded_extensions = {
        ".ttf", ".otf", ".woff", ".woff2", ".ttc", ".bdf", ".pcf",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        ".pdf", ".eps", ".ai", ".ps", ".psd", ".tif", ".tiff",
        ".zip", ".gz", ".xz", ".bz2", ".tar",
    }
    for label, root, source_root in roots:
        if root.is_symlink():
            continue
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in {".git", "target", "_dev"}
                             and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                file = Path(directory) / name
                root_attribution = source_root and file.parent == root and name in {
                    "ASSETS.md", "ATTRIBUTION.md"}
                if not (name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "OFL"))
                        or root_attribution):
                    continue
                if file.is_symlink() or not file.is_file() or file.suffix.lower() in excluded_extensions:
                    continue
                with file.open("rb") as stream:
                    raw = stream.read(MAX_NOTICES + 1)
                # Exclude binary content even when its name resembles a license.
                if any(byte < 32 and byte not in (9, 10, 12, 13) for byte in raw):
                    continue
                if len(raw) > MAX_NOTICES:
                    raise ValueError("third-party notice exceeds 32 MiB")
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                section = f"\n===== {label}/{file.relative_to(root).as_posix()} =====\n{text}\n"
                total += len(section.encode("utf-8"))
                if total > MAX_NOTICES:
                    raise ValueError("combined third-party notices exceed 32 MiB")
                sections.append(section)
    (output / "THIRD-PARTY-NOTICES.txt").write_text("".join(sections), encoding="utf-8")


def run_phase(recipe, paths, target, cargo, output, phase, cwd, arguments):
    """Use a fresh cgroup per phase; inspect it even if an OOM kills its shell."""
    name = "tapt-exporter-" + uuid.uuid4().hex
    base = ["docker", "create", "--name", name, "--pull=missing", "--platform", PLATFORM,
            "--cpus=" + recipe["cpus"], "--memory=" + recipe["memory"],
            "--memory-swap=" + recipe["memory"], "--user=" + str(os.getuid()) + ":" + str(os.getgid())]
    for key, value in recipe["environment"].items():
        base += ["-e", key + "=" + value]
    base += ["-v", str(cargo) + ":/cargo", "-v", str(target) + ":/target-app",
             "-v", str(paths["vectorcraft"]) + ":/work/vectorcraft:ro",
             "-v", str(paths["libs"]) + ":/work/libs:ro",
             "-v", str(output) + ":/build-output", "-w", cwd]
    peak = output / (phase + ".memory-peak")
    events = output / (phase + ".memory-events")
    for file in (peak, events):
        file.unlink(missing_ok=True)
    # v2 is used on GitHub's Ubuntu runners; v1 keeps local Docker runs measurable.
    script = ('"$@"; result=$?; '
              f'(cat /sys/fs/cgroup/memory.peak || '
              f'cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes) '
              f'> /build-output/{phase}.memory-peak 2>/dev/null; '
              f'cat /sys/fs/cgroup/memory.events '
              f'> /build-output/{phase}.memory-events 2>/dev/null; exit "$result"')
    subprocess.run(base + [recipe["amd64_builder_image"], "sh", "-c", script,
                          "measure", *recipe["cargo"], *arguments], check=True)
    start = time.monotonic()
    try:
        result = subprocess.run(["docker", "start", "-a", name], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, flush=True)
        state = json.loads(subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .State}}", name], text=True))
        event_text = events.read_text() if events.exists() else ""
        oom = state["OOMKilled"] or any(
            int(count) > 0 for count in re.findall(r"^oom_kill (\d+)$", event_text, re.M))
        metric = dict(phase=phase, seconds=round(time.monotonic() - start, 3),
                      returncode=state["ExitCode"] or result.returncode,
                      memory_peak_bytes=int(peak.read_text()) if peak.exists()
                      and peak.read_text().strip() else None,
                      memory_events=event_text or None, oom_killed=bool(oom))
        if phase != "build":
            metric["tests_passed"] = test_count(result.stdout)
        return metric
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True)


def compile_exporter(lock, recipe, paths, output, target, cargo, github):
    for path in (output, target, cargo):
        path.mkdir(parents=True, exist_ok=True)
    # Never leave a stale successful receipt available after a failed rebuild.
    for name in FILES:
        (output / name).unlink(missing_ok=True)
    identity = build_identity(lock, recipe, paths)
    metrics = []
    operations = [("/work/vectorcraft", recipe["build"]),
                  *[(test[0], test[1:]) for test in recipe["tests"]]]
    if len(recipe["tests"]) != 4:
        raise ValueError("exactly four native test groups are required")
    for index, (cwd, arguments) in enumerate(operations):
        phase = "build" if index == 0 else "test-" + str(index)
        metric = run_phase(recipe, paths, target, cargo, output, phase, cwd, arguments)
        metrics.append(metric)
        write_json(output / "build-metrics.json", metrics)
        if metric["returncode"] or metric["oom_killed"]:
            raise RuntimeError("exporter phase failed: " + phase)
        if metric["memory_peak_bytes"] is None:
            raise RuntimeError("exporter phase has no memory measurement: " + phase)
        if index and not metric["tests_passed"]:
            raise ValueError("exporter_rust_test_filter_exercised_no_tests: " + phase)
    # The locked build must not have changed either Cargo.lock.
    if build_identity(lock, recipe, paths) != identity:
        raise ValueError("build identity changed during compilation")
    binary = output / "vectorcraft-cli"
    shutil.copy2(target / "debug/vectorcraft-cli", binary)
    with binary.open("rb") as stream:
        header = stream.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
        raise ValueError("exporter binary is not Linux amd64 ELF")
    write_notices(lock, paths, cargo, output)
    receipt = dict(
        schema=lock["schema"], version=recipe["version"], platform=PLATFORM,
        builder_image=recipe["amd64_builder_image"],
        source_lock_sha256=sha(HERE / "source.lock.json"), identity=identity,
        binary_sha256=sha(binary), output_settings=lock["output_settings"],
        ci=dict(pipeline_uuid=None, step_uuid=None, commit=None), github=github,
        rust_tests="passed", metrics=metrics,
        qualification="implementation_pin_not_output_qualification",
    )
    write_json(output / "exporter-build.json", receipt)
    return receipt


def package(output, dist):
    dist.mkdir(parents=True, exist_ok=True)
    archive = dist / "exporter-linux-amd64.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in FILES:
            tar.add(output / name, arcname=name, recursive=False)
    (dist / "SHA256SUMS").write_text(sha(archive) + "  " + archive.name + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("only the authorized fork may publish this exporter")
    commit, run_id = os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not run_id.isdigit():
        raise ValueError("invalid GitHub build identity")
    libs_commit = read_pin()
    paths = {"vectorcraft": HERE.parent, "libs": HERE.parent.parent / "libs"}
    lock = json.loads((HERE / "source.lock.json").read_text())
    recipe = json.loads((HERE / "build.recipe.json").read_text())
    verify_sources(lock, paths, commit, libs_commit)
    github = dict(repository=REPOSITORY, commit=commit, run_id=run_id, libs_commit=libs_commit)
    compile_exporter(lock, recipe, paths, args.output.resolve(),
                     args.cache.resolve() / "target", args.cache.resolve() / "cargo", github)
    package(args.output.resolve(), args.dist.resolve())


if __name__ == "__main__":
    main()
