"""Source-evidence connectors.

Milestone 4 ships the local connectors (raw docs, local repo clones);
Milestone 5 adds the schema-only BigQuery connector. Remote GitHub and
report connectors land in later milestones.
"""


class SourceUnavailableError(Exception):
    """A source marked ``required: true`` could not be loaded."""
