import sys
from .core import MigrationError
from .runner import main

try:
    sys.exit(main())
except (MigrationError, EOFError, KeyboardInterrupt) as exc:
    print('ERRO: ' + (str(exc) or 'Operação interrompida.'), file=sys.stderr)
    sys.exit(1)
except Exception:
    # Unexpected exceptions must not leak URLs, access tokens, SQL or user data.
    print('ERRO inesperado. Preserve o diretório de execução e consulte state.json. Nenhum sucesso presumido.', file=sys.stderr)
    sys.exit(1)
