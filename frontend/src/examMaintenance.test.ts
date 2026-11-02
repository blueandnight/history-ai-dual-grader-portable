import {afterEach,expect,it,vi} from 'vitest'
import {photoCleanupReason,cleanupPhotos,deleteExam} from './examMaintenance'
import {setSession} from './api'
import type {Submission} from './history'
const paper:Submission={id:'b'.repeat(12),state:'completed',confirmed_at:'synthetic',final_total:0,max_total:8,suggested_total:8,photos:[{}]}
afterEach(()=>{vi.unstubAllGlobals();setSession(null)})
it('allows confirmed zero and discarded unconfirmed papers but blocks active and missing photos',()=>{
 expect(photoCleanupReason(paper)).toBe('')
 for(const state of ['queued','grading','recovery_pending'])expect(photoCleanupReason({...paper,state})).not.toBe('')
 for(const change of [{confirmed_at:null},{teacher_reconfirmation_required:true}])expect(photoCleanupReason({...paper,...change})).toBe('')
 for(const change of [{photos:[]},{photos_deleted:true}])expect(photoCleanupReason({...paper,...change})).not.toBe('')
})
it('uses existing same-origin CSRF transport with explicit exam ownership and never retries',async()=>{
 vi.stubGlobal('window',{location:{origin:'http://localhost'}})
 const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({ok:true})});vi.stubGlobal('fetch',fetch)
 setSession({required:true,authenticated:true,csrf_token:'synthetic'})
 await cleanupPhotos({...paper,exam_id:'e'.repeat(32)},'a'.repeat(12))
 expect(JSON.parse(fetch.mock.calls[0]![1].body)).toEqual({submission_id:paper.id,expected_assignment_id:'a'.repeat(12),expected_exam_id:'e'.repeat(32),discard_unconfirmed:false})
 expect(fetch.mock.calls[0]![1].headers['X-CSRF-Token']).toBe('synthetic')
 await expect(cleanupPhotos({...paper,state:'grading'},'a'.repeat(12))).rejects.toThrow()
 expect(fetch).toHaveBeenCalledTimes(1)
 fetch.mockRejectedValue(new Error('lost response'));await expect(deleteExam('a'.repeat(12),'合成考试','f'.repeat(64))).rejects.toThrow()
 expect(fetch).toHaveBeenCalledTimes(2)
})
