// Adapter between the existing import page and the source-record protocol.
// The server remains the authority for duplicates, validation and publication.
export function sourceBatch(source) {
  const id = source.source_id;
  const imageTypes = {png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg'};
  const contentType = imageTypes[source.file_format] || 'application/octet-stream';
  return {
    batch_id:id, account_code:source.account_code, kind:source.document_type,
    mode:source.acquisition_method === 'manual' ? 'manual' : 'upload', status:source.status,
    revision:source.revision, updated_at:source.updated_at, completeness:'partial',
    files:source.filename ? [{file_id:id,filename:source.filename,content_type:contentType,
      metadata:{template_status:'matched'}}] : [],
    rows:(source.items || []).map(item => ({row_id:item.source_item_id,
      fields:{...item.parsed_fields,kind:source.document_type}, original_fields:item.parsed_fields,
      acknowledged:item.review_status === 'confirmed', excluded:item.review_status === 'excluded',
      review_note:item.review_note, can_edit_fields:!['template','api'].includes(item.parse_method),
      source:{...(item.raw_fields?.source || {}),...(item.source_position || {}),
        ...(source.filename ? {file_id:id} : {}), origin:item.parse_method}})),
  };
}

export function createSourceRecordAdapter(request, recovery) {
  const path = id => `/source-records/${encodeURIComponent(id)}`;
  const load = async id => sourceBatch(await request(path(id)));
  async function loadAfterWrite(id) {
    try { return await load(id); }
    catch (error) {
      recovery.requiresReview = true;
      if (recovery.last) recovery.last.result = {...recovery.last.result, batchId:id};
      error.message = `操作已完成，但重新载入失败。请重新载入服务器版本，勿重复操作。${error.message}`;
      throw error;
    }
  }
  return {
    load,
    context:() => request('/source-records/context'),
    assets:query => request(`/source-records/assets?q=${encodeURIComponent(query)}`),
    async history() {
      const data = await request('/source-records?limit=20');
      return {rows:(data.sources || []).map(source => ({batch_id:source.source_id,
        account_code:source.account_code,status:source.status,revision:source.revision,updated_at:source.updated_at}))};
    },
    async createManual(account, kind) {
      const result = await request('/source-records/manual','POST',{account_code:account,document_type:kind});
      return loadAfterWrite(result.source_id);
    },
    async stageFile(account, file) {
      const result = await request(`/source-records?account_code=${encodeURIComponent(account)}&source_name=${encodeURIComponent(account)}&filename=${encodeURIComponent(file.name)}`,'POST',file,true);
      return loadAfterWrite(result.source_id);
    },
    async save(batch, rows) {
      const items = rows.map(row => {
        const fields = {...row.fields};
        if (!Object.hasOwn(row.original_fields || {}, 'kind')) delete fields.kind;
        return {source_item_id:row.row_id || null,parsed_fields:fields,
          review_status:row.excluded ? 'excluded' : row.acknowledged ? 'confirmed' : 'pending',note:row.review_note || null};
      });
      if (!items.length) return batch;
      await request(`${path(batch.batch_id)}/reviews`,'PUT',{revision:batch.revision,items});
      return loadAfterWrite(batch.batch_id);
    },
    async preview(batch) {
      if (batch.kind === 'holding') return {batch_id:batch.batch_id,revision:batch.revision,
        counts:{new:0,duplicates:0,blocked:1,pending:0},rows:[],can_publish:false,
        warnings:['持仓快照的新来源发布尚未接入，请保留草稿。']};
      const pending = batch.rows.some(row => !row.acknowledged && !row.excluded);
      if (batch.kind === 'trade' && pending) return {batch_id:batch.batch_id,revision:batch.revision,
        counts:{new:0,duplicates:0,blocked:0,pending:batch.rows.filter(row=>!row.acknowledged&&!row.excluded).length},
        rows:batch.rows.map(row=>({row_id:row.row_id,fields:row.fields,status:row.excluded?'excluded':row.acknowledged?'ready':'pending'})),
        can_publish:false,warnings:['请逐条核对记录后进行发布预检。']};
      if (batch.kind === 'cash') {
        const value = await request(`${path(batch.batch_id)}/cash-preview`,'POST',{revision:batch.revision,mode:'all'});
        let readyToken = null;
        if (value.can_publish_ready) {
          const ready = await request(`${path(batch.batch_id)}/cash-preview`,'POST',{revision:batch.revision,mode:'ready'});
          readyToken = ready.preview_token;
        }
        return {batch_id:batch.batch_id,revision:value.revision,preview_token:value.preview_token || readyToken,ready_token:readyToken,
          can_publish:value.can_publish,can_publish_ready:value.can_publish_ready,
          counts:{new:value.counts.ready,duplicates:value.counts.duplicate,blocked:value.counts.invalid,pending:value.counts.pending},
          rows:value.rows.map(item=>({row_id:item.source_item_id,status:item.status==='invalid'?'blocked':item.status}))};
      }
      const value = await request(`${path(batch.batch_id)}/trade-preview`,'POST',{revision:batch.revision});
      return {batch_id:batch.batch_id,revision:value.revision,preview_token:value.preview_token,can_publish:true,
        counts:{new:value.items.length,duplicates:0,blocked:0,pending:0},
        rows:batch.rows.map(row=>({row_id:row.row_id,fields:row.fields,status:row.excluded?'excluded':'ready'}))};
    },
    async publish(batch, preview, ready = false) {
      const suffix = batch.kind === 'cash' ? 'cash-publish' : 'trade-publish';
      const body = {revision:batch.revision,preview_token:ready?preview.ready_token:preview.preview_token};
      if (batch.kind === 'cash') body.mode = ready ? 'ready' : 'all';
      const result = await request(`${path(batch.batch_id)}/${suffix}`,'POST',body);
      return {batch:await loadAfterWrite(batch.batch_id),deferred_batch_id:result.deferred_source_id};
    },
    async remove(batch) { return request(`${path(batch.batch_id)}/delete-draft`,'POST',{revision:batch.revision}); },
    original:id => `/api/account-import${path(id)}/original`,
  };
}
