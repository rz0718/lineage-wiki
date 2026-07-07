"""Source-evidence connectors.

Milestone 4 ships the local connectors (raw docs, local repo clones);
Milestone 5 adds the schema-only BigQuery connector; the Slack report
connector fetches live alert messages as report evidence. The remote
GitHub connector lands in a later milestone.
"""


class SourceUnavailableError(Exception):
    """A source marked ``required: true`` could not be loaded."""
