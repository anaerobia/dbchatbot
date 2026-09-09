# Project conventions

## Always use a branch and pull request

Never commit or push directly to `main`. For any change:

1. Create a new branch.
2. Commit the change there.
3. Push the branch.
4. Open a pull request against `main` (e.g. with `gh pr create`).

This repo has an automated AI review workflow
(`.github/workflows/ai-review.yml`) that only runs on pull requests — a
direct push to `main` skips it entirely and gets no review.
