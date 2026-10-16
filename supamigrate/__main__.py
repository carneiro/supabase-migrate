import sys
from .core import MigrationError
from .runner import main

try:
    sys.exit(main())
except (MigrationError, EOFError, KeyboardInterrupt) as exc:
    print('ERROR: ' + (str(exc) or 'Operation interrupted.'), file=sys.stderr)
    sys.exit(1)
except Exception:
    # Unexpected exceptions must not leak URLs, access tokens, SQL or user data.
    print('Unexpected error. Preserve the run directory and see state.json. Success is not assumed.', file=sys.stderr)
    sys.exit(1)
