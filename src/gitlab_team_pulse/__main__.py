"""Allow ``python -m gitlab_team_pulse``."""

import sys

from gitlab_team_pulse.cli import main

sys.exit(main())
