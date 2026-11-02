import {it,expect,vi,afterEach} from 'vitest'
import {batchHref,fetchBatchRecords,batchTime,batchState} from './batchRecords'
import {createWorkspaceContext} from './workspaceContext'
const a='a'.repeat(12),e='e'.repeat(32),b='b'.repeat(12)
afterEach(()=>vi.unstubAllGlobals())
it('batch links preserve source and selected batch',()=>{
 const link=batchHref({batch_id:b,assignment_id:a,exam_id:e})
 expect(link).toContain('#/batch?');expect(link).toContain('batch='+b);expect(link).toContain('exam='+e)
 const quick=batchHref({batch_id:b,assignment_id:a,exam_id:e},{assignmentId:a,examId:'',mode:'quick'})
 expect(quick).toContain('mode=quick');expect(quick).not.toContain('exam=')
})
it('completed listing is explicit, scoped and GET only',async()=>{
 vi.stubGlobal('window',{location:{origin:'http://localhost'}})
 const fetch=vi.fn(async(_path:string)=>({ok:true,json:async()=>({batches:[]})}));vi.stubGlobal('fetch',fetch)
 await fetchBatchRecords({assignmentId:a,examId:e},b)
 expect(fetch.mock.calls[0]?.[0]).toContain('include_completed=1');expect(fetch.mock.calls[0]?.[0]).toContain('batch_id='+b)
 await expect(fetchBatchRecords(undefined,'bad')).rejects.toThrow()
})
it('refresh restores a batch URL and another page does not inherit its batch filter',async()=>{
 const hash='#/batch?assignment='+a+'&exam='+e+'&batch='+b
 vi.stubGlobal('window',{location:{origin:'http://localhost'}});vi.stubGlobal('location',{hash});const replace=vi.fn();vi.stubGlobal('history',{replaceState:replace})
 vi.stubGlobal('fetch',vi.fn(async(path:string)=>({ok:true,json:async()=>path.includes('assignments')?{assignments:[{id:a,name:'合成'}]}:{exam_id:e,assignment_id:a}})))
 const w=createWorkspaceContext();await w.refresh();expect(await w.restore(hash,'batch')).toBe(true);w.persist('batch');expect(replace.mock.calls.at(-1)?.[2]).toContain('batch='+b)
 w.persist('upload');expect(replace.mock.calls.at(-1)?.[2]).not.toContain('batch=')
 expect(await w.restore('#/batch?assignment='+a,'batch')).toBe(false)
})
it('status and time labels do not pretend that processing means confirmed',()=>{
 expect(batchState({batch_id:b,assignment_id:a,class_name:'',status:'completed',total_sequences:1,next_sequence:2})).toBe('处理结束');expect(batchTime('invalid')).toBe('时间未记录')
})
