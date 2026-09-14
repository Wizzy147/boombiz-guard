"""PyInstaller entry point for the Guard agent (BoombizGuardAgent.exe).

`app` uses package-relative imports, so it can't be the frozen script itself.
"""

import multiprocessing

from app.main import run

if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
