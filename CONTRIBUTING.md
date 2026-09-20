# Contributing

First off, thank you for considering contributing to fspack! It's people like you
that make fspack a better tool for everyone.

## How Can I Contribute?

### Reporting Bugs

Before creating bug reports, please check the existing issues as you might find
out that you don't need to create one. When you are creating a bug report, please
include as many details as possible:

- **Use a clear and descriptive title**
- **Describe the exact steps to reproduce**
- **Provide specific examples to demonstrate the steps**
- **Describe the behavior you observed and what behavior you expected to see**
- **Include your Python version, OS, and fspack version**

### Suggesting Enhancements

Enhancement suggestions are tracked as GitHub issues. When creating an enhancement
suggestion, please provide:

- **Use a clear and descriptive title**
- **Provide a detailed description of the suggested enhancement**
- **Explain why this enhancement would be useful**

### Pull Requests

1. Fork the repo and create your branch from `main`
2. If you've added code that should be tested, add tests
3. If you've changed APIs, update the documentation
4. Ensure the test suite passes
5. Make sure your code lints

## Development Setup

```bash
# Clone your fork
git clone https://github.com/your-username/fspack.git
cd fspack

# Install dependencies
uv sync --extra dev

# Run tests (fast, skip slow e2e tests)
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95

# Run linter
uv run ruff check src tests

# Run type checker
uv run pyrefly check

# Run the full check suite
make check
```

## Code Style

- **Python**: Follow PEP 8 enforced by `ruff`
- **Type hints**: All public APIs must have type annotations
- **Chinese comments**: Use Chinese for code comments (per project convention)
- **No dead code**: Remove unused imports and variables; the codebase is regularly cleaned
- **Package `__init__.py`**: Only do import/export, no business logic

## Commit Convention

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description
```

Types: `feat`, `fix`, `refactor`, `docs`, `style`, `test`, `chore`, `perf`, `build`, `ci`

Examples:
- `feat: add sidebar collapse with Ctrl+B shortcut`
- `fix: resolve py3.14 parser crash from bare % in help strings`
- `refactor: split packaging/__init__ into submodules`

## Pull Request Process

1. Ensure any install or build dependencies are removed before the end of the layer
   when doing a build
2. Update the README.md with details of changes to the interface
3. Run the full test suite (`make check`) and ensure all tests pass
4. PR description should reference related issues

## Testing Guidelines

- **Add tests for new features**: New code without tests will not be merged
- **Edge cases**: Test boundary conditions and error paths
- **Windows-specific tests**: Path handling, registry access, MAX_PATH limits
- **Platform-independent**: Tests should use cross-platform assertions
- **Do not relax assertions** to make flaky tests pass — fix the root cause
