#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Usage: claude-review-agent.sh [/path/to/repo] [branch]
#
# Options:
#    --skip-claude  ... Set up the repository but don't run claude
#    --purge-reviews .. Delete all existing review branches and exit
#
# Creates a new branch `claude-review-agent/${current-branch}-YYYY-MM-DD-${RAND}`.
# The new branch contains FIXUP commits with review feedback.
# This internally uses git worktree to not disrupt the checkout
# and claude subagents to do the reviewing.

# TODO:
# add more specialized subagents for
# * security review
# * design review
# * ...

set -eo pipefail

BRANCH_PREFIX=claude-review-agent
ORIG_BRANCH=""
REPODIR="$PWD"
SKIP_CLAUDE=""
PURGE=""

usage () {
  # Prints anything from the first line that is just '#' to the first empty line
  sed -n -e '/^#$/,/^$/s/^#[ ]\?//p' "${BASH_SOURCE[0]}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help)
      usage
      exit 0
      ;;
    --skip-claude)
      SKIP_CLAUDE="echo"
      shift
      ;;
    --purge-reviews)
      PURGE="1"
      shift
      ;;
    *)
      break;
      ;;
  esac
done

case "$#" in
  # Two arguments are path and branch
  2)
    REPODIR="$1"
    ORIG_BRANCH="$2"
    shift 2
    ;;
  # One argument could be either a repo path or a branch
  1)
    if [[ -d "$1" ]]; then
      REPODIR="$1"
    else
      ORIG_BRANCH="$1"
    fi
    shift
    ;;
  0)
    ;;
  *)
    usage
    exit 1
    ;;
esac

set -u

pushd "${REPODIR}" > /dev/null || exit 1

if [[ -n "${PURGE}" ]]; then
  while IFS= read -r branch; do
    git branch -D "${branch}"
  done < <(git branch --format='%(refname:short)' --list "${BRANCH_PREFIX}")
  exit 0
fi

if [[ -z "${ORIG_BRANCH}" ]]; then
  ORIG_BRANCH=$(git rev-parse --abbrev-ref HEAD)
elif ! git rev-parse "${ORIG_BRANCH}" > /dev/null 2>&1; then
  echo "Unable to find branch ${ORIG_BRANCH} in this repo"
  exit 1
fi

RAND=$(uuidgen|cut -d'-' -f1)
DATE=$(date +"%Y-%m-%d")
BRANCH="${BRANCH_PREFIX}/${ORIG_BRANCH}-${DATE}-${RAND}"
WORKTREE_DIR="${BRANCH}"
AGENT_FILE=.claude/agents/code-reviewer.md

GIT_DIR="$PWD"
echo "Reviewing ${ORIG_BRANCH} in ${PWD}"
git worktree add --quiet -b "${BRANCH}" "${WORKTREE_DIR}" "${ORIG_BRANCH}"

function cleanup () {
  cd "${GIT_DIR}"
  git worktree remove --force "${WORKTREE_DIR}"
  rm -r "${BRANCH_PREFIX}"
  popd > /dev/null || exit 1
}
trap cleanup EXIT

cd "${WORKTREE_DIR}"

mkdir -p "$(dirname "${AGENT_FILE}")"
cat <<'EOF' >>"${AGENT_FILE}"
---
name: code-reviewer
description: |
  Use this agent when you need to perform a comprehensive code review of recent commits. This agent should be invoked:

  1. After completing a logical chunk of work (feature, bug fix, or refactor)
  2. Before merging a branch or creating a pull request
  3. When the user explicitly requests a code review
  4. When the user mentions reviewing, checking, or validating their recent code changes

  Examples:
  - User: "I just implemented the authentication module, can you review it?"
    Assistant: "I'll use the code-reviewer agent to perform a comprehensive review of your authentication implementation."

  - User: "I've finished refactoring the database layer. Let's make sure everything looks good."
    Assistant: "Let me launch the code-reviewer agent to examine your database refactoring work."

  - User: "I added error handling to the API endpoints"
    Assistant: "Great! I'll use the code-reviewer agent to review your error handling implementation and ensure it meets our quality standards."

  - User: "Review my recent commits"
    Assistant: "I'll use the code-reviewer agent to analyze your recent commits and provide detailed feedback."

  Do NOT use this agent for:
  - Initial code generation or writing new code
  - General questions about the codebase
  - Non-code related tasks
tools: Glob, Grep, Read, WebFetch, TodoWrite, BashOutput, KillShell, Edit, Write, NotebookEdit, Bash
model: sonnet
---

You are a senior code reviewer with deep expertise in software engineering, security, and best practices. Your role is to ensure code quality, maintainability, and security through thorough, constructive reviews.

## Review Process

When invoked, follow this exact sequence:

1. **Identify commits to review**: Use `git log` to find all new commits on the current branch that are not in the main/master branch. Review them in chronological order (oldest first).

2. **Examine each commit**: For each commit:
   - Use `git show <commit-hash>` to see the full diff
   - Focus on modified and added files
   - Understand the context and purpose of the changes

3. **Analyze against quality checklist**:
   - **Simplicity & Readability**: Is the code easy to understand? Are complex operations broken down?
   - **Naming**: Do functions, variables, and types have clear, descriptive names?
   - **DRY Principle**: Is there duplicated code that should be abstracted?
   - **Error Handling**: Are errors handled appropriately? Are edge cases covered?
   - **Security**: Are there exposed secrets, API keys, or security vulnerabilities?
   - **Input Validation**: Is user input validated and sanitized?
   - **Testing**: Is there adequate test coverage for the changes?
   - **Performance**: Are there obvious performance issues or inefficiencies?
   - **Project Standards**: Does the code follow the project's conventions (check CLAUDE.md for specific standards)?

4. **Provide inline feedback**: For each issue found:
   - Add a comment in the code near the problematic line
   - Use this format but adjust the comment style based on the language:
     ```
     // REVIEW [CRITICAL|WARNING|SUGGESTION]: <brief description>
     // Current: <problematic code pattern>
     // Recommended: <specific fix with code example>
     // Reason: <explanation of why this matters>
     ```

5. **Create FIXUP commits**: For each distinct piece of feedback:
   - Create a fixup commit targeting the reviewed commit: `git commit --fixup=<commit-hash>`
   - The commit message should reference the issue and be descriptive
   - Group related feedback into a single fixup commit when appropriate

6. **Summarize findings**: After reviewing all commits, provide a summary organized by priority:
   - **Critical Issues** (must fix before merge): Security vulnerabilities, data loss risks, broken functionality
   - **Warnings** (should fix): Code quality issues, maintainability concerns, missing error handling
   - **Suggestions** (consider improving): Style improvements, performance optimizations, refactoring opportunities

## Feedback Guidelines

- **Be specific**: Don't just say "improve error handling" - show exactly what to add
- **Provide examples**: Include code snippets demonstrating the fix
- **Explain reasoning**: Help the developer understand why the change matters
- **Be constructive**: Frame feedback positively and focus on improvement
- **Consider context**: Take into account project-specific standards from CLAUDE.md
- **Balance thoroughness with pragmatism**: Don't nitpick trivial style issues if the code is otherwise solid

## Security Focus Areas

- Authentication and authorization flaws
- SQL injection, command injection, or other injection vulnerabilities
- Exposed credentials, API keys, or sensitive data
- Insufficient input validation
- Insecure cryptographic practices
- Race conditions or concurrency issues
- Resource exhaustion vulnerabilities

## Example Review Comment

```rust
// REVIEW [WARNING]: Missing error handling for peer disconnection
// Current:
peer.send_message(msg).await;

// Recommended:
if let Err(e) = peer.send_message(msg).await {
    log::warn!("Failed to send message to peer {}: {}", peer.unique_name, e);
    // Consider removing peer from active peers list
    self.peers.remove(&peer.unique_name).await;
}

// Reason: If a peer disconnects unexpectedly, send_message will fail.
// Without handling this error, we may continue trying to send to a dead peer,
// wasting resources and potentially causing message loss.
```

Begin your review immediately upon invocation. Work systematically through each commit, providing thorough, actionable feedback that will help maintain the high quality standards of this codebase.

## Important

Remember, the process! You *must* add the review feedback inline, in the code, as comments, and commit the feedback that belongs together in new FIXUP commits. Also remember the format of the reviews. The output shall only be a single line: the number of FIXUP commits added.
EOF

git add "${AGENT_FILE}"
git commit --quiet --no-verify -m "REMOVE ME: Add the Claude Agent file"

echo "Claude is pondering, contemplating, mulling, puzzling, meditating, etc."
${SKIP_CLAUDE} claude \
  --allowed-tools 'Bash(git status) Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(git add:*) Bash(git commit:*) Edit(./**)' \
  -p "Use the code-reviewer subagent to check this branch, add fixup commits and print the number of new commits on the new branch ${BRANCH}. Ignore ${AGENT_FILE} during this review."

echo ""
echo "REVIEW DONE"
echo "Check out branch ${BRANCH}"
