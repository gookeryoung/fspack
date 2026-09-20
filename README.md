# fspack

> One-command Python packaging — produce executables and installers.

[![PyPI](https://img.shields.io/pypi/v/fspack)](https://pypi.org/project/fspack/)
[![CI](https://github.com/gookeryoung/fspack/actions/workflows/ci.yml/badge.svg)](https://github.com/gookeryoung/fspack/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Coverage](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen.svg)
[![Release](https://img.shields.io/github/v/release/gookeryoung/fspack?include_prereleases&sort=semver)](https://github.com/gookeryoung/fspack/releases)
[![Downloads](https://img.shields.io/pypi/dm/fspack)](https://pypi.org/project/fspack/)

[English](README.md) | [简体中文](README.zh-CN.md)

fspack turns your Python project into a distributable desktop app with **one command**.
No source code changes needed — `fsp b` produces `.exe`, `fsp p` produces a Windows
installer or Linux `.deb`. Auto-dependency inference, smart wheel slimming,
pre-compiled bytecode, out of the box.

## ✨ Highlights

- **One-command workflow**: `fsp b` builds, `fsp p` packages — cargo-style two-letter shortcuts
- **Zero config**: AST-based `import` analysis infers dependencies automatically
- **Smart slimming**: Strip unused wheel sub-modules (Qt apps drop from 300MB → 80MB)
- **Cross-platform**: Windows NSIS `.exe`, Linux `.deb` + `.tar.gz`, macOS `.pkg` + `.dmg`
- **Win7 compatible**: Unique API shim DLLs — PyInstaller/Nuitka cannot do this
- **Offline mode**: `FSPACK_OFFLINE=1` for air-gapped CI/packaging machines
- **Monorepo-friendly**: `fsp b -R` recursively builds all sub-projects
- **Nuitka boost**: `--nuitka` flag for 30-50% faster startup

## ⚡ 30-Second Quick Start

```bash
pip install fspack
cd your-project          # Python project with pyproject.toml
fsp b                    # Produces dist/your-app.exe
fsp p                    # Produces dist/release/your-app-setup.exe
```

That's it. Your Python project is now a distributable desktop app that others can
double-click to run — no Python installation required on the user's machine.

### What It Looks Like

<!-- asciinema official embed → click thumbnail to play on asciinema.org -->
[![fspack workflow demo](https://asciinema.org/a/GXFTBFPReUWpeMj4.svg)](https://asciinema.org/a/GXFTBFPReUWpeMj4?autoplay=1&speed=1.5&theme=dracula)

**Click the thumbnail above** to play a real recording of `fsp init` →
`fsp b` → `fsp p`. The raw recording is in
[docs/assets/demo.cast](docs/assets/demo.cast) (asciinema v2 NDJSON).
Want to make your own? See [scripts/record_demo.py](scripts/record_demo.py).

## 🚀 Start From a Template

No project yet? Scaffold one from 18 built-in templates covering CLI, GUI, games,
science, and web scenarios:

```bash
fsp init my-app --template pyside2   # Create PySide2 GUI project
fsp init --list                      # List all available templates
```

Each template generates a packable project skeleton (`pyproject.toml` + entry script).
`cd` in and run `fsp b` immediately.

## 🔄 Competitor Comparison

| Feature | fspack | PyInstaller | Nuitka | cx_Freeze | Briefcase |
|---------|--------|-------------|--------|-----------|-----------|
| One-command build | ✅ `fsp b` | `pyinstaller --onefile` | `python -m nuitka` | `cxfreeze` | `briefcase build` |
| Auto-dependency inference | AST closure | Hooks + hidden imports | AST + runtime tracer | Config file | Manual |
| Smart slimming | By import closure | Basic strip | None | None | None |
| Cross-platform installers | Win/Linux/macOS | Executable only | Executable only | Executable only | Win/macOS |
| Win7 compatibility | ✅ API shim | ❌ | ❌ | ❌ | ❌ |
| Offline packaging | ✅ | ❌ | ❌ | ❌ | ❌ |
| Multi-entry monorepo | ✅ `-R` | Multiple commands | Multiple commands | Multiple commands | ❌ |
| Nuitka acceleration | `--nuitka` | ❌ | Nuitka only | ❌ | ❌ |

## 📦 Installation

```bash
pip install fspack
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add fspack
```

## 📖 Usage

### Single Project Build

In your Python project root (with `pyproject.toml`):

```bash
# 1. Build: produces dist/<name>.exe and dist/runtime/
fsp b

# 2. Run and verify the packaged app
fsp r

# 3. Create installer: produces dist/release/<name>-setup.exe
fsp p

# 4. Clean up dist/
fsp c
```

Specify project directory and options:

```bash
fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows
```

### Recursive Monorepo Build

```bash
fsp b -R ./workspace       # Recursively build all sub-projects
fsp p -R ./monorepo        # Recursively package all sub-projects
```

### Multi-Entry Projects

Declare multiple entry points via standard PEP 621 `[project.scripts]`:

```bash
fsp b                     # Build all declared entry executables
fsp r --entry cli         # Run the cli entry
fsp r --entry gui         # Run the gui entry
```

## 📋 Command Reference

Global options: `-V/--version` shows version, `-v/--verbose` enables DEBUG logging.

| Command | Alias | Description |
|---------|-------|-------------|
| `fsp build` | `fsp b` | Build project — produce executables and runtime |
| `fsp run` | `fsp r` | Run packaged app (native on Linux, wine on Windows) |
| `fsp clean` | `fsp c` | Clean dist/ directory |
| `fsp package` | `fsp p` | Create installers (Windows NSIS / Linux .deb + tar.gz) |
| `fsp init` | `fsp i` | Scaffold new project from templates (18 available) |
| `fsp doctor` | — | Environment diagnostics — check toolchain availability |
| `fsp cache` | — | Cache health check and cleanup (corrupted/stale/orphaned) |

```text
fsp b [project] [--mirror] [--py-version] [--target] [--nuitka] [-R] [--dry-run] [--profile] ...
fsp r [project] [--entry <name>] [--debug] [--profile] [-- <args>...]
fsp p [project] [--no-build] [--format <auto|zip|nsis|tar.gz|deb|all>] [-R]
fsp init [name] [--template <id>] [--list]
fsp doctor                # Environment diagnostics (--test validates built-in templates)
fsp cache status|clean    # Cache health check and cleanup
```

Detailed option listing: [CLI Reference](docs/cli.md).

## ⚙️ Configuration

Use the `[tool.fspack]` section in `pyproject.toml` for icons, exclusion rules,
wheel slimming, private package sources, build defaults, and extras groups.
Multi-entry declaration follows standard PEP 621 `[project.scripts]`:

```toml
[tool.fspack]
icon = "assets/app.ico"
slim-exclude = ["PySide6/translations/*"]   # Force-strip during wheel slimming

[project.scripts]
gui = "myapp.gui:main"                       # Produces gui.exe (no console window)
```

Full configuration reference: [Configuration Guide](docs/configuration.md).

## 🖥️ Platform Support

| Platform | Runtime | Installer |
|----------|---------|-----------|
| Windows | Portable Python (official embed) | NSIS `.exe` |
| Linux | python-build-standalone | `.deb` + `.tar.gz` |
| macOS | python-build-standalone | `.pkg` + `.dmg` |

**Windows target**: Cross-compile from any build machine (Linux/macOS with mingw-w64 →
`fsp b --target windows`). macOS/Linux targets require same-platform build machines
(cross-machine requests will fail explicitly; `--dry-run` preview works from anywhere).

**Win7 SP1 support**: fspack automatically injects API shim DLLs or replaces with
Win7-recompiled `python3XX.dll`, and outputs a full compatibility scan report.
Supports Python 3.9–3.14. See [Distribution Guide](docs/distribution.md).

## 🔧 CI/CD Integration

Two GitHub Actions workflow templates provided:
- [`pack-check.yml`](templates/pack-check.yml) — PR validation build
- [`release-pack.yml`](templates/release-pack.yml) — Release packaging

Copy to `.github/workflows/`, set `PROJECT_NAME` / `EXPECTED_OUTPUT` variables, done.
Full guide: [CI/CD Integration](docs/integration.md).

## 📂 Output Layout

```text
dist/
├── <name>.exe          # Executable (double-click to run)
├── runtime/            # Python runtime (portable, no installation needed)
│   └── Lib/site-packages/   # Third-party deps (slimmed + pre-compiled)
├── src/                # Your source code
└── release/            # Installers produced by `fsp p`
    ├── <name>-setup.exe           # Windows installer
    ├── <name>_<ver>_amd64.deb     # Linux .deb
    └── <name>-<ver>-linux.tar.gz  # Linux portable package
```

## 🔐 Security & Distribution

Windows executables automatically embed PE resource sections (VS_VERSIONINFO /
manifest / icon) to reduce antivirus false positives. Production distribution
recommendations including code signing: [Distribution Guide](docs/distribution.md).

## 🛠️ Development

```bash
uv sync --extra dev                                          # Install dev dependencies
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95  # Tests
uv run pyrefly check                                         # Type check
uv run ruff check src tests                                  # Lint
```

Run `make help` for all shortcuts. Architecture and module index:
[Architecture Docs](docs/architecture.rst).

## 📚 Documentation

- [CLI Reference](docs/cli.md) — All commands and options
- [Configuration Guide](docs/configuration.md) — `[tool.fspack]` options
- [Offline Packaging](docs/offline.md) — Air-gapped / intranet CI solutions
- [Distribution Guide](docs/distribution.md) — Secure distribution and Win7 compatibility
- [Architecture](docs/architecture.rst) — Build pipeline, module index, implementation details
- [CI/CD Integration](docs/integration.md) — GitHub Actions setup
- [API Reference](docs/api.rst) — Auto-generated API docs
- [Changelog](CHANGELOG.md) — Version history

## 🤝 Contributing

Contributions welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📝 License

MIT
