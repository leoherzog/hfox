"""`python -m hfox`, and the script PyInstaller freezes into the release binaries."""

# Absolute import: PyInstaller runs this file as a top-level script, where relative imports fail.
from hfox.cli.main import app

if __name__ == "__main__":
    app()
