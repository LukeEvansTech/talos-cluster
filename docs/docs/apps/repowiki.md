# Repowiki

## Purpose

`repowiki` generates an AI-written wiki for repositories, serving it with mkdocs-material off
a shared PVC. See the [AI / LLM stack](../architecture/ai-llm-stack.md) page for how the
`mkdocs` and `repowiki-gen` controllers split the work.

## Deploy gotchas

- On a fresh PVC, `/app-data/repository` doesn't exist until `repowiki-gen` has run once, and
  mkdocs refuses to start ("Config value 'docs_dir': The path '/app-data/repository' isn't an
  existing directory"). This was observed as 5 crash-loops on a new PVC. The `seed-placeholder`
  init container creates the directory and writes a placeholder `index.md` so mkdocs has
  something to serve. `generate.py` unconditionally rewrites `REPO/index.md` at the end of
  every run, and `mkdocs.yml`'s `docs_dir` points straight at the repository root, matching
  where `generate.py` writes, so the real content replaces the placeholder the first time the
  CronJob fires.
  - The init container reuses the mkdocs image rather than pulling a new one.
  - It only creates the directory and the placeholder file: `generate.py` already runs
    `git init` on the repository itself when `.git` is absent, so the init container doesn't
    need to do that too.
