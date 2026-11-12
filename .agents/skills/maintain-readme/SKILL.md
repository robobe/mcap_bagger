---
name: maintain-readme
description: Maintain this project's root README on request or after version, feature, CLI/API/config, installation/dependency, architecture, or build/test workflow changes.
---

# Maintain README

Read [the reference template](references/readme-template.md) on each invocation.
Use its main heading order and adapt its subsections to this project. Replace
sample content with verified facts; never copy placeholder commands or links.

For `$maintain-readme` without a specified change, review the whole README.
For a specified change or workflow event, inspect the relevant diff and source,
then update affected content while retaining the required structure.
Read package versions from `pyproject.toml` when relevant. Verify commands,
configuration, dependencies, examples, and local links against repository evidence.
Document implemented behavior, not unfinished plans. Preserve useful existing
content, especially operational limitations and failure handling.

Keep required sections even when information is unavailable; say it is unspecified.
Do not invent licenses, roadmap commitments, links, or commands. Report conflicting
or missing facts. Edit only root `README.md`, not application code or the template.
Leave an accurate README unchanged. Check heading order and local links, and
report what was updated and what could not be verified.

Workflow reviews happen after implementation and validation, before completion.
Internal refactors without documentation impact need no review. These instructions
run within Codex tasks; they do not monitor external edits or install event hooks.
