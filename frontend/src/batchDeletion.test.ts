import {afterEach,expect,it,vi} from 'vitest'
import {previewBatchDelete,deleteBatch,type BatchDeletePreview} from './batchDeletion'
import {setSession} from './api'
import type {Batch} from './records'
const batch:Batch={batch_id:'a'.repeat(12),assignment_id:'b'.repeat(12),exam_id:'c'.repeat(32),class_name:'',status:'paused',total_sequences:2,next_sequence:1}
const p={batch_id:batch.batch_id,assignment_id:batch.assignment_id,exam_id:batch.exam_id!,fingerprint:'d'.repeat(64),blocked:false} as BatchDeletePreview
function setup(value:unknown){vi.stubGlobal('window',{location:{origin:'http://127.0.0.1'}});setSession({required:true,authenticated:true,csrf_token:'synthetic'});vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify(value))))}
afterEach(()=>{setSession(null);vi.unstubAllGlobals()})
it('preview is scoped and read only; a mismatched response fails',async()=>{setup(p);expect(await previewBatchDelete(batch)).toEqual(p);expect(vi.mocked(fetch).mock.calls[0]![1]?.method).toBe('GET');setup({...p,batch_id:'e'.repeat(12)});await expect(previewBatchDelete(batch)).rejects.toThrow()})
it('delete needs exact preview and explicit consent; sends no score or identity',async()=>{setup({ok:true});for(const [preview,consent] of [[p,false],[{...p,blocked:true},true],[{...p,exam_id:''},true],[{...p,fingerprint:''},true]] as const)expect(()=>deleteBatch(batch,preview,consent)).toThrow();expect(fetch).not.toHaveBeenCalled();await deleteBatch(batch,p,true);expect(fetch).toHaveBeenCalledTimes(1);expect(JSON.parse(vi.mocked(fetch).mock.calls[0]![1]!.body as string)).toEqual({batch_id:batch.batch_id,assignment_id:batch.assignment_id,exam_id:batch.exam_id,confirmed:true,expected_fingerprint:p.fingerprint})})
