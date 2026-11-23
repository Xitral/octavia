"""Self-improvement: the assistant notices what it gets wrong or can't do, and builds the fix.

The loop, and who is trusted with what:

1. **Notice** (``journal``, ``watcher``): exceptions in the assistant's own code are
   recorded automatically; the brain records capability gaps, mistakes and user
   complaints with the ``note_improvement`` tool.
2. **Build** (``builder``): a separate coding agent (Claude Code through the Agent
   SDK) implements the change in its own git worktree. Every file access and
   command goes through a deterministic guard (``policy``): it can only touch the
   worktree, never protected files, and can only run tests, the linter and
   read-only git.
3. **Check** (``gate``): non-AI code re-checks the result: protected files
   untouched, lint clean, the full test suite passing. The builder's own claims are
   never trusted.
4. **Install** (``manager``): only with the user's approval (a CONFIRM tool, asked
   out loud), merged as one commit, then the assistant restarts into the same
   conversation.
5. **Recover** (``supervisor``): a small parent process restarts the assistant and
   reverts an update that crashes it on startup. Any update can also be undone.

This package is itself protected: the builder can never modify it, the
permission policy, the tool executor or the configuration.
"""
