
# Functional tests for REasy – parses / rebuilds RSZ files.

import os
import sys
import time
import unittest
from pathlib import Path

script_dir = Path(__file__).resolve().parent
project_root = next(
    (p for p in script_dir.parents
     if (p / "resources" / "data" / "dumps").is_dir()),
    None,
)
if project_root is None:
    raise RuntimeError("Could not locate project root (expecting resources/data/dumps)")

# make project packages importable
sys.path.insert(0, str(project_root))

from utils.type_registry import TypeRegistry
from file_handlers.rsz.rsz_file import RszFile

# ───────────────────────── game‑table ─────────────────────────
GAME_CONFIGS = {
    "mhwilds":{"registry_json": "rszmhwilds.json",   "scn_exts": [".21"], "usr_exts": [".3"],  "pfb_exts": [".18"]},
    "re7":    {"registry_json": "rszre7.json",       "scn_exts": [".18"], "usr_exts": [".-1"], "pfb_exts": [".16"]},
    "re3":    {"registry_json": "rszre3.json",       "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"]},
    "re3rt":  {"registry_json": "rszre3rt.json",     "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"]},
    "re8":    {"registry_json": "rszre8.json",       "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"]},
    "mhr":    {"registry_json": "rszmhrise.json",    "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"]},
    "sf6":    {"registry_json": "rszsf6.json",    "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"],
        "usr_file_exceptions": ["esf021_001_01_chain.user.2", "esf021_000_02_chain.user.2"]},
    
    "re4":    {"registry_json": "rszre4_reasy.json", "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"],
        "usr_file_exceptions": ["ch4fez0actionpropertyuserdata.user.2", "tabledefine.user.2"],
        "scn_file_exceptions": ["gimmick_st61_500.scn.20", "gimmick_st63_108.scn.20", "gimmick_st61_502.scn.20", "gimmick_st61_502_ch.scn.20"]
    },
    "re2":    {"registry_json": "rszre2.json",       "scn_exts": [".19"], "usr_exts": [".-1"], "pfb_exts": [".16"],
        "scn_file_exceptions": ["gimmick.scn (39).scn.19", "gimmick.scn (21).scn.19", "gimmick.scn (40).scn.19", "gimmick.scn (10).scn.19"]
    },
    "dmc5":    {"registry_json": "rszdmc5.json",       "scn_exts": [".19"], "usr_exts": [".-1"], "pfb_exts": [".16"],
        "scn_file_exceptions": ["em0100_viewer.scn.19", "m04_200.scn.19", "m04_200_lightweight.scn.19", "m04_300.scn.19", "m04_300_lightweight.scn.19"]
    },
    

    "re2rt":  {"registry_json": "rszre2rt.json",     "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"],
        "scn_file_exceptions": ["gimmick.scn (98).20", "gimmick.scn (10).20", "gimmick.scn (163).20", "gimmick.scn (122).20"]
    },
    "re7rt":  {"registry_json": "rszre7rt.json",     "scn_exts": [".20"], "usr_exts": [".2"],  "pfb_exts": [".17"], 
        "pfb_file_exceptions": ["em8000deadbody.pfb.17"],
        "scn_file_exceptions": [
            "levelfsm_c00.scn.20", "levelfsm_c01.scn.20",
            "levelfsm_c03_2.scn.20", "levelfsm_c03_3.scn.20",
            "levelfsm_c03_4.scn.20", "levelfsm_c04_1.scn.20",
            "levelfsm_c04_2.scn.20", "levelfsm_c04_3.scn.20",
            "levelfsm_ff040.scn.20"
        ],
    },
}

_cli_game = None
if len(sys.argv) > 1 and sys.argv[-1].lower() in GAME_CONFIGS:
    _cli_game = sys.argv.pop().lower()

class TestSCNParser(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        cls.sample_root  = script_dir         
        cls.project_root = project_root
        cls.json_dir     = project_root / "resources" / "data" / "dumps"

        if _cli_game:
            cls.game_key = _cli_game
        elif os.getenv("TEST_GAME"):
            env_game = os.getenv("TEST_GAME").lower()
            if env_game not in GAME_CONFIGS:
                raise ValueError(f"Unknown TEST_GAME={env_game}")
            cls.game_key = env_game
        else:
            cls.game_key = next(iter(GAME_CONFIGS))

        cls.cfg = GAME_CONFIGS[cls.game_key]

        print(f"[Test] script_dir     = {script_dir}")
        print(f"[Test] sample_root    = {cls.sample_root}")
        print(f"[Test] project_root   = {cls.project_root}")
        print(f"[Test] registry JSONs = {cls.json_dir}")
        print(f"[Test] Running tests for game: {cls.game_key}")

    def setUp(self):
        reg_path = self.json_dir / self.cfg["registry_json"]
        if not reg_path.is_file():
            self.fail(f"Registry JSON not found: {reg_path}")

        self.type_registry = TypeRegistry(str(reg_path))
        self.logs_dir = self.project_root / "tests" / "logs" / self.game_key
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _run_dir_tests(self, test_dir: Path, exts, ftype):
        if not test_dir.is_dir():
            self.skipTest(f"No {ftype} directory for {self.game_key}")
            return

        ignore = set(self.cfg.get(f"{ftype.lower()}_file_exceptions", []))

        to_check = [
            Path(root) / fn
            for root, _, files in os.walk(test_dir)
            for fn in files
            if fn not in ignore and any(fn.lower().endswith(ext) for ext in exts)
        ]

        if not to_check:
            self.skipTest(f"No {ftype} files for {self.game_key}")
            return

        ok = bad = 0
        for fp in to_check:
            rel = fp.relative_to(self.sample_root)
            print(f"[Test] {ftype}: {rel}")
            if self._verify_file(fp, ftype):
                ok += 1
            else:
                bad += 1

        print(f"[Result] {ftype}: {ok}/{ok+bad} passed, {bad} failed.")
        self.assertEqual(bad, 0, f"{bad} {ftype} files failed")
        
    def _verify_file(self, filepath: Path, file_type: str) -> bool:
        log_file = self.logs_dir / f"{file_type.lower()}_test_failures.log"

        def log_error(msg, errtype=""):
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {errtype} - {filepath.name}: {msg}\n")

        try:
            input_bytes = filepath.read_bytes()
        except Exception as e:
            log_error(f"Failed to read: {e}", "READ_ERROR")
            return False

        scn = RszFile()
        scn.filepath = str(filepath)
        scn.type_registry = self.type_registry
        try:
            scn.read(input_bytes)
        except Exception as e:
            log_error(f"Failed to parse: {e}", "PARSE_ERROR")
            return False

        original_resources = [scn.get_resource_string(ri) for ri in scn.resource_infos]

        built_ok = False
        for align in (True, False):
            try:
                if scn.build(special_align_enabled=align) == input_bytes:
                    built_ok = True
                    break
            except Exception as e:
                log_error(f"build(align={align}) error: {e}", f"BUILD_ERROR_{align}")

        if not built_ok:
            log_error("Round‑trip mismatch", "MISMATCH")
            return False

        md = self.type_registry.registry.get("metadata", {})
        if md.get("complete") or md.get("resources_identified"):
            scn.auto_resource_management = True
            scn.rebuild_resources()
            trimmed = {scn.get_resource_string(ri) for ri in scn.resource_infos}

            if scn.is_pfb and filepath.name.lower().endswith(".16"):
                expected = set(scn.get_resources_dynamically())
                if trimmed != expected:
                    log_error(
                        f"PFB.16 resource mismatch:\n"
                        f"  expected: {sorted(expected)}\n"
                        f"  actual:   {sorted(trimmed)}",
                        "RESOURCE_MISMATCH_PFB16",
                    )
                    return False
            else:
                if trimmed != set(original_resources):
                    log_error(
                        f"Resource‑set mismatch:\n"
                        f"  before trim: {sorted(original_resources)}\n"
                        f"  after  trim: {sorted(trimmed)}",
                        "RESOURCE_MISMATCH",
                    )
                    return False
        return True

    def test_usr_files(self):
        if not self.cfg["usr_exts"]:
            self.skipTest(f"{self.game_key} has no USER files")
        self._run_dir_tests(self.sample_root / self.game_key / "user",
                            self.cfg["usr_exts"], "USR")

    def test_pfb_files(self):
        self._run_dir_tests(self.sample_root / self.game_key / "pfb",
                            self.cfg["pfb_exts"], "PFB")

    def test_scn_files(self):
        self._run_dir_tests(self.sample_root / self.game_key / "scn",
                            self.cfg["scn_exts"], "SCN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
