"""Load and verify each source original once within a read operation."""
from hashlib import sha256


class SourceOriginals:
    def __init__(self, cur):
        self.cur = cur
        self._cache = {}
        self._verified = {}

    @staticmethod
    def _key(row):
        return (row['source_account'], row['sha256'],
                str(row['original_source_id']) if row['inline_original'] else None)

    def verify_many(self, rows):
        """Verify scoped originals in PostgreSQL without transferring their bytes."""
        keys = {self._key(row) for row in rows if row['sha256'] is not None}
        keys.difference_update(self._cache, self._verified)
        ordered = sorted(keys, key=lambda key: (key[0], key[1], key[2] or ''))
        for offset in range(0, len(keys), 100):
            batch = ordered[offset:offset + 100]
            inline = [key for key in batch if key[2] is not None]
            detached = [key for key in batch if key[2] is None]
            if inline:
                accounts, digests, ids = map(list, zip(*inline))
                self.cur.execute("""SELECT COALESCE(encode(pg_catalog.sha256(r.content), 'hex')=q.digest,false) AS verified
                    FROM unnest(%s::text[],%s::text[],%s::uuid[]) WITH ORDINALITY AS q(account,digest,id,ord)
                    LEFT JOIN public.source_records r ON r.source_id=q.id AND r.account_code=q.account AND r.sha256=q.digest
                    ORDER BY q.ord""", (accounts, digests, ids))
                self._save_verification(inline)
            if detached:
                accounts, digests = map(list, zip(*[(account, digest) for account, digest, _ in detached]))
                self.cur.execute("""SELECT COALESCE(encode(pg_catalog.sha256(
                        public.source_original_content(q.account,q.digest,NULL)), 'hex')=q.digest,false) AS verified
                    FROM unnest(%s::text[],%s::text[]) WITH ORDINALITY AS q(account,digest,ord)
                    ORDER BY q.ord""", (accounts, digests))
                self._save_verification(detached)

    def _save_verification(self, keys):
        for key, record in zip(keys, self.cur.fetchall(), strict=True):
            self._verified[key] = bool(record['verified'] if hasattr(record, 'keys') else record[0])

    def verify(self, row):
        if row['sha256'] is None:
            return not row['inline_original']
        key = self._key(row)
        if key in self._cache:
            return self._cache[key][1]
        if key not in self._verified:
            self.verify_many((row,))
        return self._verified[key]

    def read(self, row):
        digest = row['sha256']
        inline = row['inline_original']
        if digest is None:
            return None, not inline

        account = row['source_account']
        source_id = row['original_source_id']
        # Detached broker originals are immutable. Verify inline bytes per source
        # row so one inline copy cannot vouch for a different copy of the same hash.
        key = self._key(row)
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
