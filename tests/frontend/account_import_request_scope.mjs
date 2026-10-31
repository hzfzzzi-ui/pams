import assert from 'node:assert/strict';
import {createRequestScope} from '../../frontend/public/assets/js/features/account_import/request_scope.js';
import {createImportClient} from '../../frontend/public/assets/js/features/account_import/client.js';
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
{
 const wait=deferred(),calls=[];
 const scope=createRequestScope(async(...args)=>{calls.push(args);return wait.promise;});
 const read=scope.request('/context');
 assert.equal(calls[0][4].aborted,false);scope.dispose();assert.equal(calls[0][4].aborted,true);
 wait.resolve({accounts:['late']});await assert.rejects(read,e=>e.name==='AbortError');
 await assert.rejects(scope.request('/context'),e=>e.name==='AbortError');assert.equal(calls.length,1);
}
{
 const wait=deferred(),external=new AbortController();
 const scope=createRequestScope(async()=>wait.promise),read=scope.request('/assets?q=x','GET',undefined,false,external.signal);
 external.abort();wait.resolve({rows:['late']});await assert.rejects(read,e=>e.name==='AbortError');scope.dispose();
}
{
 const wait=deferred(),calls=[];
 const scope=createRequestScope(async(...args)=>{calls.push(args);return wait.promise;});
 const write=scope.request('/batches/b','PUT',{revision:1,rows:[]});
 assert.equal(scope.hasPendingWrite(),true);assert.equal(calls[0][4],undefined);
 scope.dispose();wait.resolve({revision:2});await assert.rejects(write,e=>e.name==='AbortError');
 assert.equal(calls.length,1);assert.equal(scope.hasPendingWrite(),false);
}
{
 let calls=0;
 const scope=createRequestScope(createImportClient(async()=>{calls++;return {ok:false,status:409,json:async()=>({detail:{code:'revision_conflict',message:'服务器版本已更新'}})};}));
 await assert.rejects(scope.request('/batches/b','PUT',{revision:1}),e=>e.status===409&&e.code==='revision_conflict');
 assert.equal(calls,1);scope.dispose();
}
{
 const wait=deferred(),calls=[];
 const old=createRequestScope(async(...args)=>{calls.push(args);return wait.promise;},{getBatch:()=>({batch_id:'b',revision:2})});
 const write=old.request('/batches/b/publish','POST',{revision:2,preview_token:'p'});
 old.dispose();
 const next=createRequestScope(async(...args)=>{calls.push(args);return {batch_id:'b',revision:2,status:'draft'};},{recovery:old.recovery});
 await next.request('/batches/b');
 assert.equal(next.hasPendingWrite(),true);assert.equal(next.acknowledgeRecovery(),false);
 await assert.rejects(next.request('/batches/b/publish','POST',{revision:2}),e=>e.code==='write_result_unconfirmed');
 wait.resolve({batch:{batch_id:'b',revision:3,status:'published'}});await assert.rejects(write,e=>e.name==='AbortError');
 assert.equal(next.recovery.last.outcome,'succeeded');assert.equal(next.hasPendingWrite(),false);
 assert.deepEqual(next.recovery.last.result,{batchId:'b',revision:3,status:'published'});
 assert.equal(next.recovery.requiresReview,true);assert.equal(calls.length,2,'read plus original write only');
 next.dispose();
}
{
 const scope=createRequestScope(async()=>{throw new TypeError('connection reset');},{getBatch:()=>({batch_id:'b',revision:2})});
 await assert.rejects(scope.request('/batches/b','PUT',{revision:2}),/connection reset/);
 assert.equal(scope.recovery.last.outcome,'unknown');assert.equal(scope.recovery.requiresReview,true);
 assert.equal(scope.hasPendingWrite(),false);scope.dispose();
}
console.log(JSON.stringify({readCancellation:true,externalSignal:true,lateSuppressed:true,writeNotAbortedOrRetried:true,conflictUnchanged:true,remountWriteBarrier:true,lateOutcomeMetadataOnly:true,unknownOutcomeBlocked:true}));
