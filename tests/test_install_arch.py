"""Installer architecture routing with fake executables; no packages are installed."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class InstallerArchitectureTests(unittest.TestCase):
    def run_installer(self, host_arch: str, arm64_hardware: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "install.sh"
            shutil.copy2(INSTALLER, installer)
            frontend = root / "jupyter-data/labextensions/jupyterlab-nb-analysis-bridge"
            frontend.mkdir(parents=True)
            (frontend / "package.json").write_text("{}")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            trace = root / "pip-args.txt"
            python = bin_dir / "fake-python"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -c ]; then\n"
                "  case \"$2\" in *platform.machine*) echo x86_64;; esac\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = -m ] && [ \"$2\" = pip ]; then\n"
                "  printf '%s\\n' \"$*\" > \"$INSTALL_TRACE\"\n"
                "  exit 17\n"
                "fi\n"
                "exit 0\n"
            )
            uname = bin_dir / "uname"
            uname.write_text(f"#!/bin/sh\n[ \"$1\" = -s ] && echo Darwin || echo {host_arch}\n")
            sysctl = bin_dir / "sysctl"
            sysctl.write_text(f"#!/bin/sh\necho {arm64_hardware}\n")
            for executable in (python, uname, sysctl):
                executable.chmod(0o755)
            env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "NBIDE_PYTHON": str(python), "INSTALL_TRACE": str(trace)}
            result = subprocess.run(["bash", str(installer)], cwd=root, env=env,
                                    capture_output=True, text=True, check=False)
            return trace.read_text().strip(), result.stdout + result.stderr

    def test_mixed_arch_requires_cryptography_wheel_and_explains_recovery(self):
        args, output = self.run_installer("arm64", "1")
        self.assertIn("--only-binary=cryptography", args)
        self.assertIn("conda install -c conda-forge cryptography", output)

    def test_native_arch_keeps_normal_dependency_install(self):
        args, output = self.run_installer("x86_64", "0")
        self.assertNotIn("--only-binary", args)
        self.assertIn("-m pip install .", args)


if __name__ == "__main__":
    unittest.main()
