"""Run the Danica terminal interface as a module."""

from danica import perf
from danica.tui.app import main

perf.mark("modules imported")      # /perf shows how long start-up took, phase by phase
main()
