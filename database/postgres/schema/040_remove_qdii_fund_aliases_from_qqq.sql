-- These Chinese QDII feeder funds are wrapper products, not aliases of the
-- US-listed QQQ ETF itself. Keep QQQ lookthrough/proxy logic in exposure
-- relations, not in pams_asset_alias identity matching.

DELETE FROM pams_asset_alias
WHERE asset_id = 'ETF_QQQ'
  AND alias_type = 'alias'
  AND alias_value IN (
    '华泰柏瑞纳斯达克100ETF发起式联接(QDII)I',
    '广发纳斯达克100ETF联接人民币(QDII)A',
    '建信纳斯达克100指数(QDII)A人民币',
    '建信纳斯达克100指数(QDII)C人民币'
  );
