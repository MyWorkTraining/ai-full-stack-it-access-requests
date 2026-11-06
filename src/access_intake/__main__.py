"""`python -m access_intake ...` is the same as the `intake` command."""
import sys

from .cli import main

sys.exit(main())
