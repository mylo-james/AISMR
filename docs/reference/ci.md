# Continuous integration

**AISMR checks** has one Linux job for the current recorded Vercel app. It installs
locked Python dependencies, checks source formatting/types/security, and runs:

- The saved workflow, both review gates, private media access and job-claim tests.
- Browser progress and manual-release identity checks, using Node without an npm install.
- Additive migrations and database coordination against disposable PostgreSQL.

It needs no generation credentials, FFmpeg, renderer build or application Docker
image. Runs are limited to ten minutes. New pushes cancel superseded checks for
the same PR or branch. Checks run on PRs (including drafts), `main` pushes and
manual requests. Opening a draft does not start the former full product suite.

Security Scan retains the locked runtime/hosted dependency audit and CodeQL on
PRs, main pushes and its weekly schedule. Docs Check handles documentation links.
Deployment stays in the separate manual **AISMR deployment** workflow, with its
own production environment, serialization and exact candidate identity checks.
See [AISMR deployment](../how-to/aismr-deployment.md).

The broader local suite remains available through `make test-fast`; it is not a
prerequisite for exercising the saved public walkthrough. Renderer tests, builds
and legacy evaluation checks remain available locally for changes to those paths.
Enabling live generation or rendering requires adding the relevant checks to CI
before that capability ships.

Ruff explicitly uses `E4`, `E7`, `E9` and `F`, matching the established pre-commit
contract. Black, mypy and Bandit retain their source checks. The full unit suite's
80% coverage configuration remains available for local coverage runs.
