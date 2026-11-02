import { afterEach, expect, it, vi } from 'vitest'
import { acceptedPayload, anonymousExamCandidates, applyPendingPrivacy, bulkCropChecks, artifact, assertLayout, assertOriginalExam, assertPreview, assertRuns, canSend, cropFile, groupsFromFiles, originalPhotoPayload, uploadBody, uploadReservedUntilStopped, type FormalAssignment, type Layout, type CropRun, type Preview } from './formalUpload'
import { previewBody } from './privacyEditing'
import { setSession } from './api'
const aid='a'.repeat(12),eid='e'.repeat(32),pid='d'.repeat(32),url='data:image/png;base64,AAAA'
const assignment:FormalAssignment={id:aid,name:'synthetic',layout_exams:[],sub_questions:[{id:'Q17.1',label:'17.1'},{id:'Q17.2',label:'17.2'}]}
it('keeps original-photo candidates separate from layout candidates across mode switches',()=>{
 const original={exam_id:'b'.repeat(32)},crop={exam_id:eid}
 const a={...assignment,original_photo_exams:[original,crop],layout_exams:[crop]}
 expect(anonymousExamCandidates(a,'original_photo')).toEqual([original,crop])
 expect(anonymousExamCandidates(a,'auto_crop')).toEqual([crop])
 expect(anonymousExamCandidates({...a,layout_exams:[]},'auto_crop')).toEqual([])
 expect(anonymousExamCandidates({...a,layout_exams:[]},'original_photo')).toEqual([original,crop])
 expect(anonymousExamCandidates(assignment,'original_photo')).toEqual([])
})
it('bulk privacy covers 50 papers without overwriting exceptions or replacing final consent',()=>{
 const states=Array(50).fill('unchecked');states[0]='redacted'
 const applied=applyPendingPrivacy(states,'no_identity_visible')
 expect(states[49]).toBe('unchecked');expect(applied[0]).toBe('redacted');expect(applied.slice(1)).toEqual(Array(49).fill('no_identity_visible'))
 expect(applyPendingPrivacy(states,'invalid')).toEqual(states)
 expect(canSend(applied,50,false)).toBe(false);expect(canSend(applied,50,true)).toBe(true)
 applied[49]='unchecked';expect(canSend(applied,50,true)).toBe(false)
})
const layout=():Layout=>({exam_id:eid,assignment_id:aid,answer_regions_required:true,layout_profile_id:pid,subjective_question_ids:['17.1','17.2'],layout_profile:{regions:['17.1','17.2'].map(question_id=>({question_id,region_type:'subjective_answer'}))}})
const run=():CropRun=>({status:'cropped',exam_id:eid,layout_profile_id:pid,alignment_run_id:'f'.repeat(32),crops:['17.1','17.2'].map(question_id=>({question_id,file_name:question_id+'.png'}))})
const accepted=()=>assignment.sub_questions.map(q=>({question_id:q.id,layout_profile_id:pid,data_url:url,attestation:'synthetic',provenance:{exam_id:eid}}))
const file=(name:string)=>({name,size:3} as File)
it('bulk crop review selects all crops and allows clearing',()=>{
 expect(bulkCropChecks([false,false,false,true],true)).toEqual([true,true,true,true])
 expect(bulkCropChecks([true,true,true,true],false)).toEqual([false,false,false,false])
})
it('finishes an in-flight upload after stop and resumes at the first untouched sequence',async()=>{
 const items=['one','two','three'],started:string[]=[],uploaded:number[]=[];let stop=false,release!:()=>void
 const inFlight=uploadReservedUntilStopped(items,0,async(item,index)=>{started.push(`${index+1}:${item}`);await new Promise<void>(resolve=>{release=resolve})},()=>stop,index=>uploaded.push(index))
 await Promise.resolve();expect(started).toEqual(['1:one']);stop=true;release()
 expect(await inFlight).toBe(1);expect(uploaded).toEqual([0])
 const resumed=await uploadReservedUntilStopped(items,1,async(item,index)=>{started.push(`${index+1}:${item}`)},()=>false,index=>uploaded.push(index))
 expect(resumed).toBe(3);expect(started).toEqual(['1:one','2:two','3:three']);expect(uploaded).toEqual([0,1,2])
})
afterEach(()=>{vi.unstubAllGlobals();setSession(null)})
it('preserves explicit file order and rejects incomplete/oversize groups',()=>{
 expect(groupsFromFiles([file('2.png'),file('1.png')],2,false)[0]!.files.map(f=>f.name)).toEqual(['2.png','1.png'])
 expect(()=>groupsFromFiles([file('1.png')],2,false)).toThrow('分组');expect(()=>groupsFromFiles([file('x.pdf')],1,false)).toThrow('PNG')
 expect(()=>groupsFromFiles([{name:'x.png',size:26*1024*1024} as File],1,false)).toThrow('25MiB')
})
it('never infers students or reorders pages from filenames',()=>{
 const groups=groupsFromFiles([file('01_2.png'),file('bad name.png'),file('01_1.png')],1,true)
 expect(groups).toHaveLength(3);expect(groups.map(g=>g.files[0]!.name)).toEqual(['01_2.png','bad name.png','01_1.png'])
})
it('requires an intact formal layout and all configured questions',()=>{
 assertLayout(layout(),aid)
 for(const key of ['profile_invalid','profile_hash_mismatch','pdf_hash_mismatch'] as const){const c=layout();c[key]=true;expect(()=>assertLayout(c,aid)).toThrow()}
 const c=layout();c.layout_profile=null;expect(()=>assertLayout(c,aid)).toThrow();expect(()=>assertLayout(layout(),'foreign')).toThrow()
 const missing=layout();missing.layout_profile!.regions.pop();expect(()=>assertLayout(missing,aid)).toThrow('全部')
})
it('allows original photos for an associated exam without valid ROI layout',()=>{
 const c:Layout={...layout(),answer_regions_required:false,layout_profile_id:'',layout_profile:null,profile_invalid:true}
 expect(()=>assertOriginalExam(c,aid,eid)).not.toThrow();expect(()=>assertOriginalExam(c,'foreign',eid)).toThrow();expect(()=>assertOriginalExam(c,aid,'foreign')).toThrow()
})
it('rejects partial/failed, duplicate/missing, wrong exam/profile crop runs',()=>{
 assertRuns([run()],layout())
 for(const patch of [{status:'alignment_failed'},{partial_processing:true},{exam_id:'0'.repeat(32)},{layout_profile_id:'0'.repeat(32)},{crops:[run().crops[0]!]}])expect(()=>assertRuns([{...run(),...patch}],layout())).toThrow()
 expect(()=>assertRuns([run(),run()],layout())).toThrow('重复')
})
it('only constructs same-origin crop artifact URLs',()=>{
 expect(artifact('f'.repeat(32),'17.1.png')).toContain('/api/student-auto-crop-artifact?')
 expect(artifact('f'.repeat(32),'padding_validation/17_1_crop_padded.png')).toContain('file=padding_validation%2F17_1_crop_padded.png')
 expect(()=>artifact('f'.repeat(32),'../../secret.png')).toThrow();expect(()=>artifact('https://evil','x.png')).toThrow()
})
it('explains layout mismatch and insufficient matching without allowing upload',()=>{
 expect(()=>assertRuns([{...run(),status:'alignment_failed',failure_code:'page_layout_mismatch'}],layout())).toThrow('同一版答题卡')
 expect(()=>assertRuns([{...run(),status:'alignment_failed'}],layout())).toThrow('单页/双页')
 expect(()=>assertRuns([{...run(),status:'alignment_failed',failure_code:'untrusted'}],layout())).toThrow('本批未发送模型')
})
it('preserves server crop bytes/attestation and blocks mismatched provenance',()=>{
 const result=acceptedPayload(accepted(),assignment,layout())
 expect(result.photos[0]).toMatchObject({image_role:'answer_region',data_url:url,original_data_url:url,answer_region_attestation:'synthetic'})
 expect(result.regions.map(r=>r.sub_question_ids)).toEqual([['Q17.1'],['Q17.2']])
 const crops=accepted();crops[0]!.provenance.exam_id='foreign';expect(()=>acceptedPayload(crops,assignment,layout())).toThrow()
 expect(()=>acceptedPayload(accepted().slice(0,1),assignment,layout())).toThrow()
})
const preview=():Preview=>({preview_token:'synthetic',preview_fingerprint:'synthetic-hash',prompt:'synthetic',images:[{data_url:url,sub_question_ids:['Q17.1']},{data_url:url,sub_question_ids:['Q17.2']}],answer_regions:acceptedPayload(accepted(),assignment,layout()).regions})
it('never accepts blocked/full-page previews or empty privacy consent',()=>{
 assertPreview(preview(),2);const p=preview();p.images[0]!.image_kind='full_page';expect(()=>assertPreview(p,2)).toThrow()
 expect(()=>assertPreview({...preview(),blocked:true},2)).toThrow();expect(()=>assertPreview({...preview(),preview_token:''},2)).toThrow()
 expect(canSend(['unchecked'],1,true)).toBe(false);expect(canSend(['no_identity_visible'],1,false)).toBe(false);expect(canSend(['no_identity_visible'],2,true)).toBe(false);expect(canSend(['no_identity_visible'],1,true)).toBe(true)
})
it('accepts full-page previews only for original-photo mode with no answer regions',()=>{
 const p:Preview={preview_token:'token',preview_fingerprint:'fingerprint',prompt:'synthetic',images:[{data_url:url,image_kind:'full_page',sub_question_ids:[]}],answer_regions:[]}
 expect(()=>assertPreview(p,1,'original_photo')).not.toThrow();expect(()=>assertPreview(p,1,'auto_crop')).toThrow()
 expect(()=>assertPreview({...p,answer_regions:preview().answer_regions},1,'original_photo')).toThrow()
 const settings={redactions:[[{x:.1,y:.1,width:.1,height:.1}]],corners:[null],answer_regions:[]}
 const body=previewBody(aid,'student',[url],settings,true,{gradingInputMode:'original_photo',examId:eid})
 expect(body).toMatchObject({grading_input_mode:'original_photo',exam_id:eid,answer_regions:[],dual_review_enabled:true});expect(previewBody(aid,'student',[url],settings,true,{gradingInputMode:'original_photo',examId:eid,className:'合成班'})).toMatchObject({class_name:'合成班'})
})
it('builds original-photo submissions from source pages and preserves redactions',()=>{
 const file=new File(['synthetic'],'page.png',{type:'image/png'}),p={preview_token:'token',preview_fingerprint:'fingerprint',prompt:'synthetic',images:[{data_url:url,image_kind:'full_page',source_page:1,sub_question_ids:[],sha256:'synthetic'}],answer_regions:[]}
 const payload=originalPhotoPayload({dataUrls:[url],settings:{redactions:[[{x:.1,y:.1,width:.1,height:.1}]],corners:[null],answer_regions:[]},preview:p,anomalyPages:[],templateApplied:false},[file],eid,['page.png'])
 expect(payload).toMatchObject({gradingInputMode:'original_photo',regions:[],filenames:['page.png'],photos:[{page_sequence:1,image_role:'source_page',data_url:url,ai_redactions:[{x:.1,y:.1,width:.1,height:.1}]}]})
 const body=uploadBody(assignment,'different-exam','',payload,'redacted',{batch_id:'b'.repeat(12),items:[{submission_id:'c'.repeat(12)}]},0)
 expect(body).toMatchObject({grading_input_mode:'original_photo',exam_id:eid,answer_regions:[]})
})
it('submits signed regions without identity evidence',()=>{
 const p=acceptedPayload(accepted(),assignment,layout()),approval={...p,filenames:['01.png','01.png'],preview:preview()},batch={batch_id:'b'.repeat(12),items:[{submission_id:'c'.repeat(12)}]}
 const body=uploadBody(assignment,eid,'合成班',approval,'no_identity_visible',batch,0)
 expect(body).toMatchObject({exam_id:eid,dual_review_enabled:true,ai_privacy_confirmed:true,grading_input_mode:'auto_crop',sequence:1})
 expect(body.photos).toEqual(p.photos);expect(JSON.stringify(body.photos)).not.toContain('01.png');expect(()=>uploadBody(assignment,eid,'',approval,'unchecked',batch,0)).toThrow()
})
it('sends multipart using browser boundary with the same CSRF/auth guard',async()=>{
 vi.stubGlobal('window',{location:{origin:'http://127.0.0.1:8765'},dispatchEvent:vi.fn()});vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify(run()))))
 setSession({required:true,authenticated:true,csrf_token:'synthetic'})
 await cropFile(new File(['synthetic'],'01.png',{type:'image/png'}),eid,'single_page')
 const options=vi.mocked(fetch).mock.calls[0]![1]!;expect(options.body).toBeInstanceOf(FormData)
 expect(options.headers).toMatchObject({'X-CSRF-Token':'synthetic'});expect(options.headers).not.toHaveProperty('Content-Type');expect(options.credentials).toBe('same-origin')
 expect(fetch).toHaveBeenCalledTimes(1)
 setSession({required:true,authenticated:true});await expect(cropFile(new File(['x'],'01.png'),eid,'single_page')).rejects.toThrow('重新登录');expect(fetch).toHaveBeenCalledTimes(1)
})
