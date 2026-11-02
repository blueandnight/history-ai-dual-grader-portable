import {beforeEach,it,expect,vi} from 'vitest'
import {assertContextSyntax,createWorkspaceContext} from './workspaceContext'
const a='a'.repeat(12),e='e'.repeat(32)
beforeEach(()=>{
 vi.stubGlobal('window',{location:{origin:'http://localhost'}})
 vi.stubGlobal('fetch',vi.fn(async(path:string)=>({ok:true,json:async()=>path.includes('assignments')?{assignments:[{id:a,name:'合成'}]}:{exam_id:e,assignment_id:a}})))
})
it('restores exact server exam ownership and refuses silent replacement',async()=>{
 const w=createWorkspaceContext();await w.refresh()
 expect(await w.restore('#/upload?exam='+e,'upload')).toBe(true)
 expect(w.current.value.assignmentId).toBe(a)
 expect(await w.select({assignmentId:'b'.repeat(12),examId:e})).toBe(false)
 expect(w.current.value.examId).toBe(e);expect(w.error.value).toBeTruthy()
})
it('invalid and empty explicit identifiers fail closed',()=>{
 for(const h of ['#/upload?exam=bad','#/upload?assignment=','#/objective?assessment=x','#/objective?job=bad','#/upload?mode=wrong'])expect(()=>assertContextSyntax(h)).toThrow()
})
it('quick anonymous needs no exam, formal context cannot leak into it',async()=>{
 const w=createWorkspaceContext();await w.refresh()
 expect(await w.select({assignmentId:a,examId:'',mode:'quick'})).toBe(true)
 expect(await w.select({assignmentId:a,examId:e,mode:'quick'})).toBe(false)
})

it('invalid navigation invalidates earlier pending selections',async()=>{
 const w=createWorkspaceContext();await w.refresh()
 let finish:(v:unknown)=>void=()=>{}
 vi.stubGlobal('fetch',vi.fn(()=>new Promise(resolve=>{finish=resolve})))
 const old=w.select({assignmentId:a,examId:e})
 expect(await w.restore('#/history?exam=bad','history')).toBe(false)
 finish({ok:true,json:async()=>({exam_id:e,assignment_id:a})})
 expect(await old).toBe(false);expect(w.current.value.examId).toBe('');expect(w.error.value).toBeTruthy()
})
it('quick old links preserve the explicitly selected standard without accepting a formal exam',async()=>{
 const w=createWorkspaceContext();await w.refresh()
 expect(await w.restore('#/quick?assignment='+a+'&mode=quick','quick')).toBe(true)
 expect(w.current.value.assignmentId).toBe(a)
 expect(await w.restore('#/quick?exam='+e,'quick')).toBe(false)
})

it('old assessment links use the saved source, but explicit wrong assignments block',async()=>{
 const w=createWorkspaceContext();await w.refresh();await w.select({assignmentId:a,examId:e})
 vi.stubGlobal('fetch',vi.fn(async(path:string)=>({ok:true,json:async()=>path.includes('/assessments/')?{source_exam_id:e}:{exam_id:e,assignment_id:a}})))
 expect(await w.restore('#/objective?assessment='+e,'objective')).toBe(true)
 expect(await w.restore('#/objective?assignment='+'b'.repeat(12)+'&assessment='+e,'objective')).toBe(false)
})

it('only objective navigation retains an explicit task selector',async()=>{
 const w=createWorkspaceContext();await w.refresh();await w.select({assignmentId:a,examId:e});
 vi.stubGlobal('location',{hash:'#/objective?assignment='+a+'&exam='+e+'&assessment='+e+'&job='+e});const replaceState=vi.fn();vi.stubGlobal('history',{replaceState});
 w.persist('objective');expect(replaceState.mock.calls[0]![2]).toContain('job='+e);
 w.persist('history');expect(replaceState.mock.calls[1]![2]).not.toContain('job=');
})
