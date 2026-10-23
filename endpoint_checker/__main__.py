import os
import sys

from endpoint_checker.cli import main

try:
    sys.exit(main())
except BrokenPipeError:  # output piped into `head` etc.
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(0)
