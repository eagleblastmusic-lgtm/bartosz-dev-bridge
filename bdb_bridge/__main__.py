from __future__ import annotations

from . import cli
from .background_start_diagnostics import install_background_start_diagnostics

install_background_start_diagnostics(cli)

if __name__ == "__main__":
    cli.main()
