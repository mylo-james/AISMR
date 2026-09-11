# Continuous integration

The main CI workflow has two independent Linux jobs:

- **Python checks** installs `uv.lock` with development and hosted dependencies,
  runs Ruff, Black, Bandit and mypy, requires 80% unit-test coverage, validates
  migrations against disposable PostgreSQL, and loads the evaluation fixtures
  without provider calls. Coverage is retained as a GitHub artifact.
- **Web and renderer** checks the browser and release helper, runs renderer tests,
  builds its TypeScript, and verifies the local application Docker image builds.
  The image is not pushed or deployed.

Full CI runs on pushes to `main`, review-ready pull requests, or a manual workflow
run on a selected branch. Unfinished draft PRs skip these two jobs; a skip is not
validation evidence. Superseded runs are canceled. Before approving a candidate,
inspect a completed full run for its exact commit.

Security Scan retains dependency auditing and CodeQL on every PR, main push and
weekly schedule. Docs Check validates changed documentation links separately.
Neither workflow can release the application.

Ruff's configured rule set (`E4`, `E7`, `E9`, `F`) matches the existing Ruff 0.14
pre-commit contract. It is explicit because Ruff 0.16 expanded its defaults.
Adopting additional lint families requires a separate reviewed change. Black,
mypy and Bandit retain their existing checks.

Deployment stays in the separate manual **AISMR deployment** workflow, with its
own production environment and serialization. Its source, project, configuration
and staged deployment identity checks must pass before promotion. See
[AISMR deployment](../how-to/aismr-deployment.md).
