import {afterEach,expect,it,vi} from 'vitest'
import {prepareQuickPrivacyPreview,quickUploadBody} from './privacyEditing'
import {applyPendingPrivacy,canSend} from './formalUpload'
vi.mock('./imagePreparation',()=>({readFileDataUrl:vi.fn(async()=> 'data:image/png;base64,AAAA'),compressImage:vi.fn(async()=> 'data:image/png;base64,AAAA')}))
vi.mock('./api',()=>({request:vi.fn(),getSession:vi.fn(),isOperator:vi.fn()}))
import {request} from './api'
const files=[new File(['synthetic'],'01.png',{type:'image/png'})]
const preview={preview_token:'signed',preview_fingerprint:'fingerprint',prompt:'synthetic',images:[{data_url:'data:image/png;base64,AAAA',source_page:1,sha256:'hash'}],answer_regions:[]}
afterEach(()=>vi.clearAllMocks())
it('prepares signed previews only, without upload or teacher consent',async()=>{
 vi.mocked(request).mockResolvedValue(preview)
 const approval=await prepareQuickPrivacyPreview('assignment','第1份',files,true)
 expect(request).toHaveBeenCalledTimes(1)
 expect(request).toHaveBeenCalledWith('/api/ai-payload-preview','POST',expect.objectContaining({assignment_id:'assignment',dual_review_enabled:true,answer_regions:[]}))
 expect(()=>quickUploadBody('assignment','第1份',files,approval,'no_identity_visible',false,true)).toThrow()
})
it.each([{...preview,blocked:true},{...preview,images:[]},{...preview,preview_token:''},{...preview,answer_regions:[{photo_index:1}]}])('blocks incomplete or incompatible previews',async(result)=>{
 vi.mocked(request).mockResolvedValue(result)
 await expect(prepareQuickPrivacyPreview('assignment','第1份',files,true)).rejects.toThrow()
})
it('discarded contexts cannot adopt asynchronous preview responses',async()=>{
 vi.mocked(request).mockResolvedValue(preview)
 let checks=0
 await expect(prepareQuickPrivacyPreview('assignment','第1份',files,true,()=>{if(++checks===2)throw Error('context changed')})).rejects.toThrow('context changed')
})
it('51 papers use one bulk status action, preserving exceptions and final consent',()=>{
 const states=Array<string>(51).fill('unchecked');states[0]='redacted'
 const applied=applyPendingPrivacy(states,'no_identity_visible')
 expect(applied[0]).toBe('redacted');expect(applied.slice(1)).toEqual(Array(50).fill('no_identity_visible'))
 expect(canSend(applied,51,false)).toBe(false);expect(canSend(applied,51,true)).toBe(true)
 expect(states[1]).toBe('unchecked')
})
