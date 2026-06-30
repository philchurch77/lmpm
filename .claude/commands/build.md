# The Build — New Feature Workflow

You are running The Build: the standard end-to-end workflow for adding a new feature to this project.

Work through each stage in order. Spawn each agent when instructed and wait for their report before moving to the next stage.

---

## Stage 1 — Plan with Theo

Spawn Theo with the feature the user has described. Ask Theo to produce:
- A step-by-step implementation plan
- Which existing files will be touched
- Any risks or design decisions to flag before writing code

Present Theo's plan to the user and ask: "Does this plan look right? Anything to change before we start?"

---

## Stage 2 — Architecture check with Ada *(if needed)*

If Theo's plan involves any of the following, spawn Ada to review the proposed design before any code is written:
- A new Django app
- New models or significant changes to existing models
- A new permission structure or access-control pattern

Skip this stage for additions to existing views or templates that don't change the data model.

---

## Stage 3 — Implement

Implement the plan step by step, following the approach Theo outlined. Check off each step as it completes.

---

## Stage 4 — Simplify with Les

Once implementation is complete, spawn Les on the files that changed. Ask Les to flag anything oversized, duplicated, or hard to follow. Fix any High or Medium findings before moving on.

---

## Stage 5 — QA with Vera

Spawn Vera to test the completed feature from a real user's perspective. Vera should verify the golden path, edge cases, and any regression risks in nearby features.

---

## Finish

Report back:
- What was built
- What Les flagged and whether it was fixed
- What Vera found
- Whether the feature is ready to hand back, or what still needs attention
