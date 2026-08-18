"""Enable ``python -m brains ...`` as an alias for the ``brains-ai`` binary.

The OS-service installers (``brains.service``) launch the supervised stack
with ``<python> -m brains serve-all`` because it is the most portable exec
form: it works from any interpreter that has brains installed (including the
Windows ``pythonw.exe`` used to avoid a console window) without depending on
the ``brains-ai`` console script being on ``PATH``.
"""

from brains.cli.app import app

if __name__ == "__main__":
    app()
