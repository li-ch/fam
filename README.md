# fam

The Free Agent Market.

## Requirements

- Python 3
- [uv](https://docs.astral.sh/uv/)
- GNU Make (e.g. Git Bash, WSL, macOS, or Linux)

## Getting started

Clone the repository and install dependencies for all packages under `libs/`:

```bash
git clone https://github.com/li-ch/fam.git
cd fam
make install
```

`make install` creates a `.venv` in the repo root and editable-installs each project that has a `pyproject.toml` under `libs/`.

Activate the environment:

```bash
# Unix-like
source .venv/bin/activate

# Windows (cmd)
.venv\Scripts\activate.bat

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

## Development

From the repository root you can run checks across all libraries:

```bash
make format   # formatters
make lint     # linters
make test     # test suites
make all      # lint, format, and test
```

To work on one library, use that library’s directory (each with its own `Makefile`):

```bash
cd libs/<package-name>
make format
make lint
make test
```

Optional: run a single test file or pass extra pytest options (supported by each library’s `Makefile`):

```bash
TEST=path/to/test.py make test
```

Other useful root targets:

```bash
make lock          # refresh lockfiles
make lock-upgrade  # upgrade dependencies and refresh lockfiles
```

