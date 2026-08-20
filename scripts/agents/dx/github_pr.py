"""Compatibility imports for the former pull-request publisher."""

from .github_branch import (
    BRANCH_PUBLICATION_FILENAME,
    BRANCH_PUBLICATION_SCHEMA_VERSION,
    GitHubBranchError,
    publish_reviewed_branch,
)


GitHubPullRequestError = GitHubBranchError
PULL_REQUEST_FILENAME = BRANCH_PUBLICATION_FILENAME
PULL_REQUEST_SCHEMA_VERSION = BRANCH_PUBLICATION_SCHEMA_VERSION
publish_reviewed_pull_request = publish_reviewed_branch
