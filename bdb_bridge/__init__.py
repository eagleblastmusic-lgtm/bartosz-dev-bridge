from __future__ import annotations

import os


if os.environ.get("BDB_LIGHTWEIGHT_NATIVE_HOST") != "1":
    from . import _public_api as _api
    from ._public_api import *

    __all__ = _api.__all__
else:
    # The packaged Native Host imports only its explicit composition root. Avoid
    # loading the service, repository-analysis and CLI stacks into every browser
    # message process.
    __all__: list[str] = []
