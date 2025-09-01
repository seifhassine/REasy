#!/usr/bin/env python3
import os
import sys
import time
import unittest
from pathlib import Path

script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent

sys.path.insert(0, str(project_root))

from file_handlers.motbank.motbank_file import MotbankFile


KNOWN_GAMES = [
	"re4", "re2", "re2rt", "re8", "re3", "re3rt", "reresistance",
	"re7", "re7rt", "mhwilds", "mhr", "dmc5", "sf6", "o2", "dd2"
]


def _discover_games_with_motbank_map(sample_root: Path) -> dict[str, str]:
	mapping: dict[str, str] = {}
	for child in sample_root.iterdir():
		if child.is_dir() and (child / "motbank").is_dir():
			mapping[child.name.lower()] = child.name
	return dict(sorted(mapping.items(), key=lambda kv: kv[0]))

def _resolve_actual_game_folder(sample_root: Path, lower_game: str) -> str:
	actual = _discover_games_with_motbank_map(sample_root).get(lower_game)
	return actual or lower_game


_cli_game = None
_lower_to_actual = _discover_games_with_motbank_map(script_dir)
_available_games = KNOWN_GAMES

args_copy = list(sys.argv)
for i in range(len(args_copy) - 1, 0, -1):
	a = args_copy[i]
	al = a.lower()
	if al.startswith("--game="):
		_cli_game = al.split("=", 1)[1]
		sys.argv.pop(i)
		break
	elif al == "--game" and i + 1 < len(args_copy):
		_cli_game = args_copy[i + 1].lower()
		sys.argv.pop(i + 1)
		sys.argv.pop(i)
		break

if _cli_game is None and len(sys.argv) > 1 and sys.argv[-1].lower() in _available_games:
	_cli_game = sys.argv.pop().lower()

if _cli_game is None and len(sys.argv) > 1:
	last = sys.argv[-1]
	if not last.startswith('-') and not last.lower().endswith('.py'):
		_cli_game = sys.argv.pop().lower()


class TestMotbankRoundTrip(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.sample_root = script_dir
		cls.project_root = project_root

		if _cli_game:
			cls.game_key = _resolve_actual_game_folder(cls.sample_root, _cli_game)
		elif os.getenv("TEST_GAME") and os.getenv("TEST_GAME").lower() in _available_games:
			cls.game_key = _resolve_actual_game_folder(cls.sample_root, os.getenv("TEST_GAME").lower())
		else:
			default_lower = _available_games[0] if _available_games else None
			cls.game_key = _resolve_actual_game_folder(cls.sample_root, default_lower) if default_lower else None

		print(f"[Test] script_dir     = {script_dir}")
		print(f"[Test] sample_root    = {cls.sample_root}")
		print(f"[Test] project_root   = {cls.project_root}")
		print(f"[Test] Running tests for game: {cls.game_key}")

	def setUp(self):
		self.logs_dir = self.project_root / "tests" / "logs" / (self.game_key or "_none_")
		self.logs_dir.mkdir(parents=True, exist_ok=True)

	def _run_dir_tests(self, test_dir: Path):
		if self.game_key is None:
			self.skipTest("No games with MOTBANK samples found")
			return

		if not test_dir.is_dir():
			self.skipTest(f"No MOTBANK directory for {self.game_key}")
			return

		to_check = [
			Path(root) / fn
			for root, _, files in os.walk(test_dir)
			for fn in files
			if ".motbank" in fn.lower()
		]

		if not to_check:
			self.skipTest(f"No MOTBANK files for {self.game_key}")
			return

		ok = bad = 0
		for fp in to_check:
			rel = fp.relative_to(self.sample_root)
			print(f"[Test] MOTBANK: {rel}")
			if self._verify_file(fp):
				ok += 1
			else:
				bad += 1

		print(f"[Result] MOTBANK: {ok}/{ok+bad} passed, {bad} failed.")
		self.assertEqual(bad, 0, f"{bad} MOTBANK files failed")

	def _verify_file(self, filepath: Path) -> bool:
		log_file = self.logs_dir / "motbank_test_failures.log"

		def log_error(msg, errtype=""):
			ts = time.strftime("%Y-%m-%d %H:%M:%S")
			with open(log_file, "a", encoding="utf-8") as f:
				f.write(f"[{ts}] {errtype} - {filepath.name}: {msg}\n")

		try:
			input_bytes = filepath.read_bytes()
		except Exception as e:
			log_error(f"Failed to read: {e}", "READ_ERROR")
			return False

		m = MotbankFile()
		try:
			m.read(input_bytes)
		except Exception as e:
			log_error(f"Failed to parse: {e}", "PARSE_ERROR")
			return False

		try:
			rebuilt = m.write()
		except Exception as e:
			log_error(f"Failed to build: {e}", "BUILD_ERROR")
			return False

		if rebuilt != input_bytes:
			log_error("Round-trip mismatch", "MISMATCH")
			return False

		return True

	def test_motbank_files(self):
		self._run_dir_tests(self.sample_root / (self.game_key or "_none_") / "motbank")


if __name__ == "__main__":
	unittest.main(verbosity=2)
