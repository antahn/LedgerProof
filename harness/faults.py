"""Fault taxonomy + injector (Phase 2). All 13, no exceptions.

DUPLICATE             same event N times, serially      -> event.id dedupe works
DUPLICATE_OBJECT      2 event.ids, same (type,obj.id)   -> second dedupe key is real
CONCURRENT_DUPLICATE  same event on N parallel conns    -> THE RACE FINDER
REORDER               release out of generation order   -> order-independent handlers
DELAY                 latency past tolerance window     -> timeouts; stale sigs rejected
DROP                  never deliver                     -> reconciler catches it
RESPOND_500           force ingest to 500               -> retry path; retry is safe
TAMPER_BODY           flip a byte after signing         -> verification fails closed
TRUNCATE_BODY         cut the payload                   -> verification fails closed
STALE_TIMESTAMP       back-date t                       -> replay protection
DOWNGRADE_SCHEME      send only v0                      -> no scheme downgrade
PARTIAL_WRITE         SIGKILL worker mid-transaction    -> atomicity; no half-post
SLOW_LORIS            hold the connection open          -> ingest doesn't wedge
"""
