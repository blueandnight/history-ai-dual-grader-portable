import {describe,it,expect} from 'vitest'
import {contextFromHash,examHref,routeFromHash} from './navigation'
const teacher={required:true,authenticated:true,role:'teacher' as const}
describe('exam workflow navigation',()=>{
 it('carries exact exam and class context across views',()=>{const context={assignmentId:'a'.repeat(12),examId:'b'.repeat(32),className:'合成 班&1'};const href=examHref('history',context);expect(contextFromHash(href)).toEqual(context);expect(routeFromHash(href,teacher)).toBe('history')})
 it('rejects malformed IDs without granting restricted views',()=>{expect(contextFromHash('#/upload?assignment=../secret&exam=bad')).toMatchObject({assignmentId:'',examId:''});expect(routeFromHash('#/system?assignment='+ 'a'.repeat(12),teacher)).toBe('system')})
 it('keeps existing bookmarked upload routes',()=>{expect(routeFromHash('#/upload',teacher)).toBe('upload');expect(examHref('upload',{assignmentId:'',examId:''})).toBe('#/upload')})
})
