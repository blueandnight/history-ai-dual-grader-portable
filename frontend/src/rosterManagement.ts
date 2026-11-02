import { request } from './api'
export type ImportCounts={new_count:number;existing_count:number;duplicate_count:number;missing_count:number;conflict_count:number}
export type RosterImport={filename:string;data_url:string;default_class_name:string}
export async function rosterFile(file:File,className:string):Promise<RosterImport>{
 if(!/\.(csv|tsv|xlsx)$/i.test(file.name)||!file.size||file.size>10*1024*1024)throw new Error('请选择不超过10MB的非空CSV、TSV或XLSX花名册。')
 const bytes=new Uint8Array(await file.arrayBuffer());let text='';for(let i=0;i<bytes.length;i+=8192)text+=String.fromCharCode(...bytes.subarray(i,i+8192))
 return {filename:file.name,data_url:'data:application/octet-stream;base64,'+btoa(text),default_class_name:className.trim()}
}
export function previewRoster(payload:RosterImport){return request<{preview:ImportCounts;classes:string[]}>('/api/import-roster','POST',{...payload,preview_only:true})}
export function importRoster(payload:RosterImport){return request<{message:string;classes:string[]}>('/api/import-roster','POST',payload)}
export type StudentEdit={student_id?:string;class_name?:string;student_no?:string;display_name:string;enabled?:boolean}
export function saveStudent(p:StudentEdit){if(!p.display_name.trim()||(!p.student_id&&!p.class_name?.trim()))throw new Error('请填写班级和姓名。');return request('/api/roster-student','POST',p)}
