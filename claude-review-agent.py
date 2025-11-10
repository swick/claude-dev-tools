#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

# TODO:
# add more specialized subagents for
# * security review
# * design review
# * ...

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print(
        "Error: PyYAML is required. Install it with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

BRANCH_PREFIX = "claude-review-agent"

ALLOWED_TOOLS = (
    "Bash(git status) "
    "Bash(git diff:*) "
    "Bash(git log:*) "
    "Bash(git show:*) "
    "Bash(git add:*) "
    "Bash(git commit:*) "
    "Edit(./**)"
)

PROMPT_TEMPLATE = """
Use the code-reviewer subagent to check this branch,
add fixup commits and print the number of new commits on the new branch {branch}.
"""

AGENT_CONTENT = """---
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
"""


def run_command(
    cmd: list[str], cwd: Optional[Path] = None, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def get_branch(repo_dir: Path, ref: str = "HEAD") -> str:
    """Get the branch name for a given ref (commit-ish).

    Args:
        repo_dir: Path to git repository
        ref: Git ref (branch name, HEAD, etc.). Default: HEAD

    Returns:
        The branch name

    Raises:
        subprocess.CalledProcessError if ref doesn't exist or not a git repo
    """
    result = run_command(
        ["git", "rev-parse", "--abbrev-ref", "--verify", ref],
        cwd=repo_dir,
    )
    return result.stdout.strip()


def purge_review_branches(repo_dir: Path) -> None:
    """Delete all review branches."""
    result = run_command(
        ["git", "branch", "--format=%(refname:short)", "--list", f"{BRANCH_PREFIX}/*"],
        cwd=repo_dir,
    )
    branches = result.stdout.strip().split("\n")
    for branch in branches:
        if branch:  # Skip empty lines
            print(f"Deleting branch: {branch}")
            run_command(["git", "branch", "-D", branch], cwd=repo_dir)


def create_review_branch(repo_dir: Path, orig_branch: str) -> tuple[Path, str]:
    """Create a new review branch using git worktree."""
    rand = str(uuid.uuid4()).split("-")[0]
    date = datetime.now().strftime("%Y-%m-%d")
    branch = f"{BRANCH_PREFIX}/{orig_branch}-{date}-{rand}"
    worktree_dir = repo_dir / branch

    print(f"Reviewing {orig_branch} in {repo_dir}")
    run_command(
        [
            "git",
            "worktree",
            "add",
            "--quiet",
            "-b",
            branch,
            str(worktree_dir),
            orig_branch,
        ],
        cwd=repo_dir,
    )

    return worktree_dir, branch


def cleanup_review_branch(repo_dir: Path, worktree_dir: Path, branch: str) -> None:
    """Clean up the worktree and any empty parent directories."""
    try:
        run_command(
            ["git", "worktree", "remove", "--force", str(branch)],
            cwd=repo_dir,
            check=False,
        )

        # Remove the worktree directory if it still exists
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)

        # Remove empty parent directories up to repo_dir
        current = worktree_dir.parent
        while current != repo_dir and current.is_relative_to(repo_dir):
            try:
                if current.exists() and not any(current.iterdir()):
                    current.rmdir()
                    current = current.parent
                else:
                    break
            except OSError:
                # Directory not empty or other error, stop cleanup
                break
    except Exception as e:
        print(f"Warning during cleanup: {e}", file=sys.stderr)


def parse_agent_yaml_to_json(agent_content: str) -> str:
    """Parse YAML agent definition and convert to JSON for --agents argument.

    The agent content has YAML frontmatter between --- markers, followed by
    the prompt content in markdown.
    """
    lines = agent_content.strip().split("\n")

    # Find the frontmatter between --- markers
    if not lines[0].startswith("---"):
        raise ValueError("Agent content must start with ---")

    # Find the end of frontmatter
    frontmatter_end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            frontmatter_end = i
            break

    if frontmatter_end is None:
        raise ValueError("Agent content must have closing --- for frontmatter")

    # Parse YAML frontmatter
    frontmatter_lines = lines[1:frontmatter_end]
    frontmatter_yaml = "\n".join(frontmatter_lines)
    frontmatter = yaml.safe_load(frontmatter_yaml)

    # Get the prompt content (everything after the second ---)
    prompt_lines = lines[frontmatter_end + 1 :]
    prompt = "\n".join(prompt_lines).strip()

    # Build the agent JSON structure
    agent = {
        "name": frontmatter.get("name"),
        "description": frontmatter.get("description"),
        "tools": frontmatter.get("tools"),
        "model": frontmatter.get("model"),
        "prompt": prompt,
    }

    # Create the JSON array with single agent
    agents_json = json.dumps([agent])

    return agents_json


def run_claude_review(worktree_dir: Path, branch: str, dry_run: bool) -> None:
    """Run the Claude review agent."""
    # Parse agent YAML and convert to JSON
    agents_json = parse_agent_yaml_to_json(AGENT_CONTENT)

    prompt = PROMPT_TEMPLATE.format(branch=branch)

    cmd = [
        "claude",
        "--allowed-tools",
        ALLOWED_TOOLS,
        "--agents",
        agents_json,
        "-p",
        prompt,
    ]

    if dry_run:
        print("Would execute command:")
        print(f"  cd {shlex.quote(str(worktree_dir))}")
        print(f"  {' '.join(shlex.quote(arg) for arg in cmd)}")
        return

    print("Claude is pondering, contemplating, mulling, puzzling, meditating, etc.")

    try:
        result = run_command(cmd, cwd=worktree_dir)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"Error running claude: {e}", file=sys.stderr)
        raise


def main() -> int:
    """Main entry point."""

    parser = argparse.ArgumentParser(
        description="""Run a Claude code review agent on a git branch

Creates a new branch `claude-review-agent/${current-branch}-YYYY-MM-DD-${RAND}`.
The new branch contains FIXUP commits with review feedback.
This internally uses git worktree to not disrupt the checkout and claude subagents to do the reviewing.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "repo_path",
        nargs="?",
        default=os.getcwd(),
        help="Path to the git repository (default: current directory)",
    )
    parser.add_argument(
        "branch",
        nargs="?",
        default="HEAD",
        help="Branch to review (default: current branch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the claude command that would be executed without running it",
    )
    parser.add_argument(
        "--purge-reviews",
        action="store_true",
        help="Delete all existing review branches and exit",
    )
    args = parser.parse_args()

    # Resolve repository directory
    repo_dir = Path(args.repo_path).resolve()
    if not repo_dir.is_dir():
        print(f"Error: {repo_dir} is not a directory", file=sys.stderr)
        return 1

    # Determine the branch
    try:
        orig_branch = get_branch(repo_dir, args.branch)
    except subprocess.CalledProcessError:
        print(
            f"Error: Unable to find branch {args.branch} in this repo", file=sys.stderr
        )
        return 1

    # Handle --purge-reviews
    if args.purge_reviews:
        try:
            purge_review_branches(repo_dir)
        except subprocess.CalledProcessError as e:
            print(f"Error purging review branches: {e}", file=sys.stderr)
            return 1
        return 0

    worktree_dir = None
    branch = None
    try:
        # Create review branch and worktree
        worktree_dir, branch = create_review_branch(repo_dir, orig_branch)

        # Run Claude review
        run_claude_review(worktree_dir, branch, args.dry_run)

        print()
        print(f"The review is done. Check out branch {branch}")

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    finally:
        if worktree_dir and branch:
            cleanup_review_branch(repo_dir, worktree_dir, branch)


if __name__ == "__main__":
    sys.exit(main())
