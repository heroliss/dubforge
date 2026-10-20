"""演示脚本的标准库安全检查；不导入产品、不启动服务、不调用 FFmpeg。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_demo as prepare
import serve_demo


class DemoPreparationChecks(unittest.TestCase):
    def test_only_new_direct_child_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(prepare, "RUNS", Path(temporary)):
            accepted = prepare.checked_folder(Path(temporary) / "new")
            self.assertEqual(accepted.parent, Path(temporary).resolve())
            for path in (Path(temporary), Path(temporary) / "a" / "nested", Path(temporary).parent / "outside"):
                with self.assertRaises(ValueError):
                    prepare.checked_folder(path)
            accepted.mkdir()
            with self.assertRaises(ValueError):
                prepare.checked_folder(accepted)

    def test_failed_atomic_replace_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            path.write_bytes(b"original")
            with patch.object(prepare.os, "replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    prepare.atomic_bytes(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_generated_media_has_only_lavfi_inputs_and_bounded_threads(self):
        command = prepare.media_command("ffmpeg", Path("own-video.mp4.tmp"))
        inputs = [command[i + 1] for i, part in enumerate(command) if part == "-i"]
        self.assertEqual(len(inputs), 4)
        self.assertTrue(all(value.startswith(("color=", "anullsrc=")) for value in inputs))
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertEqual(command[command.index("-t") + 1], "16")
        self.assertIn("-n", command)
        self.assertEqual(command[-1], "own-video.mp4.tmp")

    def test_config_does_not_copy_connections_or_credentials(self):
        values = prepare.demo_config(Path("demo"))
        self.assertEqual(values["llm"]["profiles"], [])
        self.assertEqual(values["download"]["cookies_path"], "")
        self.assertEqual(values["server"]["port"], 8611)
        for backend in ("indextts25", "gptsovits"):
            self.assertEqual(values["tts"][backend]["url"], "http://127.0.0.1:1")

    def test_environment_is_explicit_and_debug_override_is_removed(self):
        original_path = list(sys.path)
        original_bytecode = sys.dont_write_bytecode
        try:
            with patch.dict(os.environ, {"DUBFORGE_DEBUG_DATADIR": "private-data"}):
                prepare.isolated_environment(Path("safe-demo"))
                self.assertNotIn("DUBFORGE_DEBUG_DATADIR", os.environ)
                self.assertEqual(os.environ["DUBFORGE_DATA_ROOT"], str(Path("safe-demo/fixture-data")))
                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        finally:
            sys.path[:] = original_path
            sys.dont_write_bytecode = original_bytecode

    def test_manual_lines_and_version_choices_make_no_completion_claim(self):
        self.assertEqual(len(prepare.LINES), 6)
        self.assertTrue(all(0 <= start < end <= prepare.SECONDS and en and zh
                            for start, end, en, zh in prepare.LINES))
        self.assertEqual({row["dubbing"] for row in prepare.VERSIONS}, {False, True})
        self.assertTrue(all("status" not in row and "result" not in row for row in prepare.VERSIONS))

    def test_serve_rejects_unprepared_or_foreign_fixture(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(prepare, "RUNS", Path(temporary)):
            folder = Path(temporary) / "test"
            folder.mkdir()
            fixture = {"schema": prepare.MARKER, "synthetic": True,
                       "data_root": "fixture-data", "config": "fixture-config.yaml"}
            (folder / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "缺失"):
                serve_demo.load_fixture(folder)
            (folder / "fixture-data").mkdir()
            (folder / "fixture-data/dubforge.db").write_bytes(b"test only")
            (folder / "fixture-config.yaml").write_text("{}", encoding="utf-8")
            self.assertEqual(serve_demo.load_fixture(folder), fixture)
            fixture["schema"] = "not-a-demo"
            (folder / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不是"):
                serve_demo.load_fixture(folder)


if __name__ == "__main__":
    unittest.main()
