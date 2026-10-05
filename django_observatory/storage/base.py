class ObservabilityBackend:
    """Storage contract. The pipeline only ever talks to this interface, so a remote
    sink (OpenSearch, ClickHouse, HTTP collector) is a subclass, not a redesign.

    ``kind`` is one of KINDS; rows are plain JSON-safe dicts, already redacted.
    """

    KINDS = ("event", "request", "trace", "span", "exception", "query", "external",
             "security", "audit", "metric")

    def write(self, kind, rows):
        """Persist a batch. Must be atomic per call (the pipeline retries whole batches)."""
        raise NotImplementedError

    def write_batch(self, grouped):
        """Persist {kind: rows}. Override to make the whole batch one transaction."""
        for kind, rows in grouped.items():
            self.write(kind, rows)

    def query(self, kind):
        """Return a queryable collection for ``kind`` (a QuerySet for the ORM backend)."""
        raise NotImplementedError

    def delete_before(self, retention_kind, timestamp, batch_size=2000):
        """Delete rows older than ``timestamp`` in bounded batches. Returns rows deleted."""
        raise NotImplementedError

    def get_statistics(self):
        """{kind: {"rows": int, "oldest": datetime | None}}"""
        raise NotImplementedError
