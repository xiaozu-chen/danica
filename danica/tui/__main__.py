"""Run the Danica terminal interface as a module."""

import sys

from danica import frozen
frozen.dispatch(sys.argv)          # packaged exe started as a helper process (subagent, plugin, game...): run it and exit

from danica import perf
from danica.tui.app import main

perf.mark("modules imported")      # /perf shows how long start-up took, phase by phase
main()
