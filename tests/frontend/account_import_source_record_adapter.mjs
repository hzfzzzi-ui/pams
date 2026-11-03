import assert from 'node:assert/strict';
import {createSourceRecordAdapter} from '../../frontend/public/assets/js/features/account_import/source_record_adapter.js';
const source = {source_id:'s',account_code:'bank',document_type:'cash',acquisition_method:'file',status:'draft',revision:2,filename:'a.csv',file_format:'csv',items:[{source_item_id:'i',parse_method:'template',parsed_fields:{net_cash:'12'},review_status:'pending',raw_fields:{},source_position:{row:1}}]};
{
 const calls=[], recovery={requiresReview:false,last:{}};
 const api=async(path,method,body)=>{calls.push({path,method,body});if(path.endsWith('/reviews')) return {revision:3};return {...source,revision:3,items:[{...source.items[0],review_status:'confirmed'}]};};
 const adapter=createSourceRecordAdapter(api,recovery),batch=await adapter.load('s');
 batch.rows[0].acknowledged=true;
 const result=await adapter.save(batch,batch.rows);
 assert.equal(result.rows[0].acknowledged,true);
 assert.deepEqual(calls[1].body.items[0].parsed_fields,{net_cash:'12'});
 assert.ok(calls.every(x=>!x.path.startsWith('/batches')));
}
{
 const recovery={requiresReview:false,last:{outcome:'succeeded',result:{}}};
 const api=async(path,method)=>{if(method==='POST') return {source_id:'new'};throw new Error('offline');};
 const adapter=createSourceRecordAdapter(api,recovery);
 await assert.rejects(adapter.createManual('bank','cash'),/操作已完成/);
 assert.equal(recovery.requiresReview,true);assert.equal(recovery.last.result.batchId,'new');
}
{
 const calls=[],recovery={requiresReview:false};
 const api=async(path,method,body)=>{calls.push({path,body});return body.mode==='all' ? {revision:2,counts:{ready:1,pending:1,duplicate:1,invalid:0},rows:[],can_publish:false,can_publish_ready:true} : {preview_token:'ready-token'};};
 const adapter=createSourceRecordAdapter(api,recovery);
 const preview=await adapter.preview({batch_id:'s',kind:'cash',revision:2,rows:[]});
 assert.equal(preview.can_publish_ready,true);assert.equal(preview.preview_token,'ready-token');
 assert.equal(preview.counts.duplicates,1);assert.equal(calls[1].body.mode,'ready');
}

{
 const calls=[], recovery={requiresReview:false};
 const api=async(path,method,body)=>{
   calls.push({path,method,body});
   if(path.endsWith('/holding-preview')) return {revision:2,holdings:[{}],preview_token:'scope-token'};
   if(path.endsWith('/holding-publish')) return {published:1};
   return {...source,document_type:'holding',status:'published'};
 };
 const adapter=createSourceRecordAdapter(api,recovery);
 const batch={batch_id:'s',kind:'holding',revision:2,rows:[{row_id:'i',acknowledged:false}]};
 assert.equal((await adapter.preview(batch)).can_publish,false);
 assert.equal(calls.length,0);
 batch.rows[0].acknowledged=true;
 const scope={revision:2,as_of:'2026-01-02',member_count:1,cash_amount:'12',note:'Synthetic',confirmed:true};
 const preview=await adapter.preview(batch,scope);
 await adapter.publish(batch,preview);
 assert.deepEqual(calls[0],{path:'/source-records/s/holding-preview',method:'POST',body:{revision:2,scope}});
 assert.deepEqual(calls[1],{path:'/source-records/s/holding-publish',method:'POST',body:{revision:2,scope,preview_token:'scope-token'}});
}
