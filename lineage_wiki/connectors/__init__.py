"""Source-evidence connectors.

Milestone 4 ships the local connectors (raw docs, local repo clones).
BigQuery, remote GitHub, and report connectors land in later milestones.
"""


class SourceUnavailableError(Exception):
    """A source marked ``required: true`` could not be loaded."""
