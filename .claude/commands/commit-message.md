---
name: commit-message
description: Generate a structured commit message for recent changes
disable-model-invocation: true
---

!`git diff HEAD`

Review the changes above and generate a commit message following this format:

type(scope): short summary under 72 chars

- Bullet point explaining what changed and why
- Another bullet if needed

Types: feat, fix, refactor, docs, chore, test

Keep it factual and concise. Do not include what files changed, only what and why.
