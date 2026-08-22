---
paths:
  - "tests/**/*.py"
description: Evidence and regression expectations for Hydra tests.
---

# Testing evidence

Use deterministic assertions for configuration, routing, boundaries, and
governance whenever possible. Tests involving models must use pinned rubric or
fixture expectations and must not call a live model or network service.

Report the command, result, and scope of verification. Do not claim that a
broader behavior passed from a narrow test. Preserve existing tests unless the
approved requirement itself changes.
