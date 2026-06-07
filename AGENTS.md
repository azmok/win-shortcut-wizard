# AGENTS.md (Project-specific)
# Universal rules (Startup Protocol / HARD STOP / Post-Edit Self-Check) live in global ~/.gemini/AGENTS.md.
# This file contains project-specific overrides only.

## Files to ALWAYS read at startup
.antigravity/notouch.md / .antigravity/potential-risks.md / .antigravity/bug-history.md / .antigravity/sessions.md

## Protected files (editing requires explicit approval — HARD STOP)
# TODO: List project-specific protected files here. Examples:
# - src/lib/db.ts
# - src/config/constants.ts

## DB / Storage Sanctuary
Destructive operations on the production DB / Storage are forbidden.
Strictly follow the pre-deploy backup-branch workflow.
