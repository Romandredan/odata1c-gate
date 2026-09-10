"""`python -m odata1c` — то же самое, что консольный скрипт `odata1c` (SPEC §11.1): пакет
доступен и там, где точка входа `[project.scripts]` не установлена (запуск из рабочей копии,
`spawn_detached` в `odata1c.daemon` — дочерний процесс демона запускается именно так, а не через
скрипт `odata1c`, чтобы не зависеть от того, что он есть в PATH)."""

import sys

from odata1c.cli import main

if __name__ == "__main__":
    sys.exit(main())
