# CI and pre-commit

## A job in each repository

[`examples/gitlab-ci.yml`](https://github.com/jwklijnsma/ansreview/blob/main/examples/gitlab-ci.yml)
has a job for your Ansible repo's `.gitlab-ci.yml`. It needs a runner that can
reach your Ollama server; set `OLLAMA_HOST` as a CI/CD variable.

```yaml
ansreview:
  stage: test
  image: python:3.12-slim
  variables:
    ANSREVIEW_MODEL: "qwen2.5-coder:14b"
    GIT_DEPTH: "0"            # diff mode needs history
  before_script:
    - apt-get update -qq && apt-get install -y -qq git >/dev/null
    - pip install -q "ansreview[lint] @ git+https://github.com/jwklijnsma/ansreview.git"
    - '[ -f requirements.yml ] && ansible-galaxy collection install -r requirements.yml || true'
  script:
    - |
      if [ -n "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" ]; then
        git fetch -q origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
        ansreview --diff "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" -f md,gitlab -o .ansreview
      else
        ansreview -f md,gitlab -o .ansreview
      fi
  artifacts:
    when: always
    paths: [.ansreview/report.md]
    reports:
      codequality: .ansreview/gl-code-quality-report.json
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
      when: never
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
```

In a merge request it reviews only the changed lines, and the findings show up
in GitLab's **Code Quality** widget. The job fails when a finding is at or above
`fail_on` (default `high`).

## One central job for all merge requests

The same file has a second job, `ansreview-merge-requests`. Put **only** that job
in one project (for example `platform/ansreview`) and add a pipeline schedule
there (**Build › Pipeline schedules**, e.g. `*/10 * * * *`). Each run looks at the
open merge requests of the watched projects, reviews new commits and keeps one
comment per merge request up to date. The other repositories need no CI change.

CI/CD variables of that project:

| Variable | |
|---|---|
| `GITLAB_TOKEN` | Masked; group or project access token, scope `api`, on the watched projects |
| `OLLAMA_HOST` | Your Ollama server, reachable from the runner |

The job runs `ansreview watch-mrs --once` and keeps its checkouts in the CI
cache. Losing that cache is harmless: the comment remembers which commit was
reviewed. See [GitLab merge requests](../usage/merge-requests.md).

## pre-commit

A fast local gate without the model, from
[`examples/pre-commit-config.yaml`](https://github.com/jwklijnsma/ansreview/blob/main/examples/pre-commit-config.yaml).
Add the model in CI.

```yaml
repos:
  - repo: local
    hooks:
      - id: ansreview
        name: ansreview (lint + AR checks)
        entry: ansreview --no-llm --diff HEAD -q
        language: system
        pass_filenames: false
        types: [yaml]
```
