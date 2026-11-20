"""Launch entry used by the local preview config. Delegates to the site server."""

import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(__file__), "..", "serve.py"), run_name="__main__")
