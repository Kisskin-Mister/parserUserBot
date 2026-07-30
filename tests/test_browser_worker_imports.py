from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_browser_worker_imports_in_fresh_interpreter() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import browser_worker; "
                "import browser_worker.worker; "
                "import vacancy_pipeline.repository; "
                "print('IMPORT_OK')"
            ),
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IMPORT_OK"
