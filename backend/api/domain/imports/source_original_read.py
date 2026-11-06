"""Load and verify each source original once within a read operation."""
from hashlib import sha256


class SourceOriginals:
    def __init__(self, cur):
        self.cur = cur
        self._cache = {}

    def read(self, row):
        digest = row['sha256']
        inline = row['inline_original']
        if digest is None:
            return None, not inline

        account = row['source_account']
        source_id = row['original_source_id']
        # Detached broker originals are immutable. Verify inline bytes per source
        # row so one inline copy cannot vouch for a different copy of the same hash.
        key = (account, digest, source_id if inline else None)
        if key not in self._cache:
            if inline:
                self.cur.execute(
                    'SELECT content FROM public.source_records WHERE source_id=%s AND account_code=%s AND sha256=%s',
                    (str(source_id), account, digest),
                )
            else:
                self.cur.execute(
                    'SELECT public.source_original_content(%s,%s,NULL) AS content', (account, digest),
                )
            record = self.cur.fetchone()
            content = None if record is None else (record['content'] if hasattr(record, 'keys') else record[0])
            verified = content is not None and sha256(content).hexdigest() == digest
            self._cache[key] = (content if verified else None, verified)
        return self._cache[key]
