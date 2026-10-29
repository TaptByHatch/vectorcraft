"""Offline contract tests. No network, Rust toolchain or Docker daemon required."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_exporter as build


class ExporterTests(unittest.TestCase):
    platform = "linux/amd64"
    image = "rust:1.95-slim-bookworm@sha256:6f9e63259f12e1e599296f5ecfed2bae46de4af0ee0525dd8b89c046e236d5c5"
    machine = b"\x3e\x00"

    def setUp(self):
        scratch = Path(os.environ.get("GITHUB_WORKSPACE", build.HERE.parent)) / "_dev"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="exporter-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {name: self.root / name for name in ("vectorcraft", "libs")}
        self.lock = json.loads((build.HERE / "source.lock.json").read_text())
        self.recipe = json.loads((build.HERE / "build.recipe.json").read_text())
        for name, path in self.paths.items():
            path.mkdir()
            (path / "Cargo.lock").write_text(name + " lock\n")
            (path / "LICENSE-MIT").write_text(name + " copyright and MIT license\n")
        self.output, self.target, self.cargo = [self.root / name for name in ("out", "target", "cargo")]
        self.output.mkdir()
        (self.target / "debug").mkdir(parents=True)
        binary = self.target / "debug/vectorcraft-cli"
        binary.write_bytes(b"\x7fELF\x02\x01" + bytes(12) + self.machine + b"synthetic-test-binary")
        binary.chmod(0o755)
        self.github = dict(repository=build.REPOSITORY, commit="a" * 40,
                           run_id="123", libs_commit="b" * 40)

    def metric(self, recipe, paths, target, cargo, output, phase, cwd, arguments, *, platform):
        self.assertEqual(platform, self.platform)
        return dict(phase=phase, seconds=0.1, returncode=0, memory_peak_bytes=4096,
                    memory_events="oom_kill 0\n", oom_killed=False, tests_passed=2)

    def compile(self):
        return build.compile_exporter(self.lock, self.recipe, self.paths, self.output,
                                      self.target, self.cargo, self.github, self.platform)

    def test_identity_has_exact_worker_fields_and_actual_cargo_hashes(self):
        identity = build.build_identity(self.lock, self.recipe, self.paths, self.platform)
        self.assertEqual(set(identity), {"platform", "builder_image", "recipe", "sources",
                                         "cargo_locks", "output_settings"})
        self.assertEqual(identity["platform"], self.platform)
        self.assertEqual(identity["builder_image"], self.image)
        self.assertEqual(identity["recipe"], self.recipe)
        for actual, source in zip(identity["sources"], self.lock["sources"]):
            self.assertEqual(actual, {k: v for k, v in source.items() if k != "cache"})
        self.assertEqual(identity["cargo_locks"], {
            name: build.sha(path / "Cargo.lock") for name, path in self.paths.items()})
        (self.paths["libs"] / "Cargo.lock").write_text("changed")
        self.assertNotEqual(identity, build.build_identity(self.lock, self.recipe, self.paths, self.platform))

    def test_architecture_changes_only_platform_and_builder_identity(self):
        amd64 = build.build_identity(self.lock, self.recipe, self.paths)
        arm64 = build.build_identity(self.lock, self.recipe, self.paths, "linux/arm64")
        self.assertEqual(amd64["platform"], "linux/amd64")
        self.assertEqual(amd64["builder_image"], self.recipe["amd64_builder_image"])
        self.assertEqual(arm64, amd64 | {"platform": "linux/arm64", "builder_image": ExporterArm64Tests.image})

    def test_cli_passes_selected_platform_to_compiler_and_package(self):
        args = ["build_exporter.py", "--platform", self.platform, "--cache", str(self.root / "cache"),
                "--output", str(self.output), "--dist", str(self.root / "dist")]
        env = {"GITHUB_REPOSITORY": build.REPOSITORY, "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123"}
        with patch("sys.argv", args), patch.dict(os.environ, env):
            with patch.object(build, "verify_sources"), patch.object(build, "compile_exporter") as compiler:
                with patch.object(build, "package") as package:
                    build.main()
        self.assertEqual(compiler.call_args.args[-1], self.platform)
        self.assertEqual(package.call_args.args, (self.output, self.root / "dist", self.platform))

    def test_unsupported_platform_is_rejected_before_docker(self):
        with patch.object(build.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported exporter platform"):
                build.run_phase(self.recipe, self.paths, self.target, self.cargo, self.output,
                                "build", "/work/vectorcraft", self.recipe["build"], platform="linux/riscv64")
        run.assert_not_called()

    def test_all_five_exact_recipe_phases_receipt_and_allowlisted_archive(self):
        with patch.object(build, "run_phase", side_effect=self.metric) as phase:
            receipt = self.compile()
        calls = phase.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0].args[-2:], ("/work/vectorcraft", self.recipe["build"]))
        for call, test in zip(calls[1:], self.recipe["tests"]):
            self.assertEqual(call.args[-2:], (test[0], test[1:]))
        self.assertEqual(receipt["github"], self.github)
        self.assertEqual(receipt["platform"], self.platform)
        self.assertEqual(receipt["builder_image"], self.image)
        self.assertEqual(receipt["identity"], build.build_identity(
            self.lock, self.recipe, self.paths, self.platform))
        self.assertEqual(receipt["rust_tests"], "passed")
        self.assertEqual(receipt["binary_sha256"], build.sha(self.output / "vectorcraft-cli"))
        self.assertEqual(receipt["source_lock_sha256"], build.sha(build.HERE / "source.lock.json"))
        self.assertEqual(receipt["qualification"], "implementation_pin_not_output_qualification")
        self.assertEqual(receipt["metrics"], json.loads((self.output / "build-metrics.json").read_text()))
        (self.output / "private-font.ttf").write_text("must not ship")
        (self.output / "artwork.svg").write_text("must not ship")
        (self.output / "runtime.tar.gz").write_text("must not ship")
        dist = self.root / "dist"
        build.package(self.output, dist, self.platform)
        archive = dist / ("exporter-" + self.platform.replace("/", "-") + ".tar.gz")
        with tarfile.open(archive) as tar:
            self.assertEqual(tar.getnames(), list(build.FILES))
            self.assertEqual(len(tar.getnames()), 4)
            self.assertTrue(all(item.isfile() for item in tar))
            self.assertTrue(tar.getmember("vectorcraft-cli").mode & 0o111)
            notices = tar.extractfile("THIRD-PARTY-NOTICES.txt").read().decode("utf-8")
            self.assertIn("vectorcraft copyright and MIT license", notices)
            self.assertIn("libs copyright and MIT license", notices)
            self.assertNotIn("must not ship", notices)
        self.assertEqual((dist / "SHA256SUMS").read_text(), build.sha(archive) + "  " + archive.name + "\n")

    def test_notices_collect_source_and_cargo_license_text_without_assets_or_private_paths(self):
        expected = {
            "vectorcraft/ASSETS.md": "source asset credits",
            "libs/ATTRIBUTION.md": "library attribution",
            "vectorcraft/vendor/example/COPYING.txt": "vendored copying terms",
            "libs/assets/fonts/OFL.txt": "public font license text only",
            "cargo/registry/src/public-index/example-1.0/LICENSE-APACHE": "registry license",
            "cargo/git/checkouts/example/revision/NOTICE.md": "git dependency notice",
        }
        for relative, text in expected.items():
            file = self.root / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
        excluded = [
            "vectorcraft/.git/LICENSE", "vectorcraft/target/NOTICE", "libs/_dev/COPYING",
            "cargo/registry/src/public-index/example-1.0/target/NOTICE",
            "libs/assets/fonts/private-font.ttf", "vectorcraft/assets/artwork.svg",
            "libs/assets/OFL-font.ttf", "vectorcraft/assets/LICENSE-logo.svg",
            "vectorcraft/tests/fixtures/example.pdf",
        ]
        for relative in excluded:
            file = self.root / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("do not distribute " + relative)
        (self.paths["libs"] / "NOTICE-binary").write_bytes(b"not text\x00binary")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "LICENSE").write_text("symlink secret")
        (self.paths["vectorcraft"] / "linked-directory").symlink_to(outside, target_is_directory=True)
        (self.paths["libs"] / "LICENSE-linked").symlink_to(outside / "LICENSE")
        build.write_notices(self.lock, self.paths, self.cargo, self.output)
        notices = (self.output / "THIRD-PARTY-NOTICES.txt").read_text()
        for label, text in expected.items():
            self.assertIn("===== " + label + " =====", notices)
            self.assertIn(text, notices)
        for source in self.lock["sources"]:
            self.assertIn(source["repository"], notices)
            self.assertIn(source["commit"], notices)
        self.assertIn("Tapt exporter modifications", notices)
        self.assertIn("text spacing", notices)
        self.assertIn("normalized continuous process-color", notices)
        for private in (str(self.root), "do not distribute", "symlink secret", "not text"):
            self.assertNotIn(private, notices)

    def test_notices_skip_symlinked_cargo_source_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "LICENSE").write_text("symlink secret")
        (self.cargo / "registry").mkdir(parents=True)
        (self.cargo / "registry/src").symlink_to(outside, target_is_directory=True)
        build.write_notices(self.lock, self.paths, self.cargo, self.output)
        self.assertNotIn("symlink secret", (self.output / "THIRD-PARTY-NOTICES.txt").read_text())

    def test_notices_fail_closed_when_combined_text_exceeds_consumer_limit(self):
        (self.paths["vectorcraft"] / "LICENSE-large").write_text("a" * 800)
        (self.paths["libs"] / "NOTICE-large").write_text("b" * 800)
        with patch.object(build, "MAX_NOTICES", 1024):
            with self.assertRaisesRegex(ValueError, "combined third-party notices"):
                build.write_notices(self.lock, self.paths, self.cargo, self.output)
        self.assertFalse((self.output / "THIRD-PARTY-NOTICES.txt").exists())

    def test_each_zero_test_group_is_rejected_and_no_stale_success_survives(self):
        for number in range(1, 5):
            with self.subTest(group=number):
                (self.output / "exporter-build.json").write_text("stale successful receipt")

                def metric(*args, **kwargs):
                    result = self.metric(*args, **kwargs)
                    if result["phase"] == "test-" + str(number):
                        result["tests_passed"] = 0
                    return result

                with patch.object(build, "run_phase", side_effect=metric):
                    with self.assertRaisesRegex(ValueError, "exercised_no_tests"):
                        self.compile()
                self.assertFalse((self.output / "exporter-build.json").exists())
                self.assertEqual(len(json.loads((self.output / "build-metrics.json").read_text())), number + 1)

    def test_failed_oom_or_unmeasured_phase_stops_publication(self):
        for overrides in ({"returncode": 101}, {"oom_killed": True}, {"memory_peak_bytes": None}):
            with self.subTest(overrides=overrides):
                def metric(*args, **kwargs):
                    return self.metric(*args, **kwargs) | overrides

                with patch.object(build, "run_phase", side_effect=metric) as phase:
                    with self.assertRaises(RuntimeError):
                        self.compile()
                self.assertEqual(phase.call_count, 1)
                self.assertFalse((self.output / "exporter-build.json").exists())

    def test_wrong_binary_architecture_is_rejected(self):
        wrong_machine = b"\xb7\x00" if self.machine == b"\x3e\x00" else b"\x3e\x00"
        for header in (b"not an ELF", b"\x7fELF\x02\x01" + bytes(12) + wrong_machine):
            with self.subTest(header=header):
                (self.target / "debug/vectorcraft-cli").write_bytes(header)
                with patch.object(build, "run_phase", side_effect=self.metric):
                    with self.assertRaisesRegex(ValueError, self.platform.split("/")[1] + " ELF"):
                        self.compile()
                self.assertFalse((self.output / "exporter-build.json").exists())

    def test_test_parser_requires_positive_passes_not_filtered_or_ignored(self):
        self.assertEqual(build.test_count("test result: ok. 0 passed; 99 ignored; 12 filtered out"), 0)
        self.assertEqual(build.test_count("test result: FAILED. 2 passed; 1 failed"), 0)
        self.assertEqual(build.test_count("test result: ok. 2 passed\ntest result: ok. 3 passed"), 5)

    def test_verify_every_patched_file_without_patch_replay(self):
        lock = copy.deepcopy(self.lock)
        for source in lock["sources"]:
            path = self.paths[source["name"]]
            source["patched_files"] = {"Cargo.lock": build.sha(path / "Cargo.lock")}
        with patch.object(build, "git", side_effect=lambda path, *args:
                          self.github["commit" if path == self.paths["vectorcraft"] else "libs_commit"]):
            build.verify_sources(lock, self.paths, self.github["commit"], self.github["libs_commit"])
            for name in self.paths:
                file = self.paths[name] / "Cargo.lock"
                original = file.read_bytes()
                file.write_text("tampered")
                with self.assertRaisesRegex(ValueError, "patched file hash mismatch: " + name):
                    build.verify_sources(lock, self.paths, self.github["commit"], self.github["libs_commit"])
                file.write_bytes(original)
        with patch.object(build, "git", return_value="c" * 40):
            with self.assertRaisesRegex(ValueError, "fork revision mismatch"):
                build.verify_sources(lock, self.paths, self.github["commit"], self.github["libs_commit"])

    def test_docker_caps_mounts_pinned_image_metrics_and_oom_inspection(self):
        def fake_run(command, **kwargs):
            if command[:2] == ["docker", "start"]:
                (self.output / "test-1.memory-peak").write_text("12345\n")
                (self.output / "test-1.memory-events").write_text("oom_kill 1\n")
            return subprocess.CompletedProcess(command, 0, "test result: ok. 3 passed\n")

        with patch.object(build.subprocess, "run", side_effect=fake_run) as run:
            with patch.object(build.subprocess, "check_output", return_value='{"OOMKilled": false, "ExitCode": 0}'):
                metric = build.run_phase(self.recipe, self.paths, self.target, self.cargo, self.output,
                                         "test-1", "/work/vectorcraft", self.recipe["tests"][0][1:],
                                         platform=self.platform)
        command = run.call_args_list[0].args[0]
        for option in ("--cpus=2", "--memory=4g", "--memory-swap=4g", self.platform,
                       self.image, "--user=" + str(os.getuid()) + ":" + str(os.getgid()),
                       str(self.paths["libs"]) + ":/work/libs:ro",
                       str(self.paths["vectorcraft"]) + ":/work/vectorcraft:ro"):
            self.assertIn(option, command)
        self.assertEqual(command[-len(self.recipe["cargo"] + self.recipe["tests"][0][1:]):],
                         self.recipe["cargo"] + self.recipe["tests"][0][1:])
        self.assertEqual(run.call_args_list[-1].args[0][:3], ["docker", "rm", "-f"])
        self.assertEqual(metric["memory_peak_bytes"], 12345)
        self.assertEqual(metric["tests_passed"], 3)
        self.assertTrue(metric["oom_killed"])

    def test_shell_oom_has_no_stale_memory_peak_and_records_failure(self):
        (self.output / "build.memory-peak").write_text("stale")
        with patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], 137, "")):
            with patch.object(build.subprocess, "check_output", return_value='{"OOMKilled": true, "ExitCode": 137}'):
                metric = build.run_phase(self.recipe, self.paths, self.target, self.cargo, self.output,
                                         "build", "/work/vectorcraft", self.recipe["build"], platform=self.platform)
        self.assertTrue(metric["oom_killed"])
        self.assertEqual(metric["returncode"], 137)
        self.assertIsNone(metric["memory_peak_bytes"])


class ExporterArm64Tests(ExporterTests):
    platform = "linux/arm64"
    image = "rust:1.95-slim-bookworm@sha256:d81582a30689ffafaa04e2a9e117d39cc5032cb12eee8d6a5f26b1f215c1e3e8"
    machine = b"\xb7\x00"


if __name__ == "__main__":
    unittest.main()
