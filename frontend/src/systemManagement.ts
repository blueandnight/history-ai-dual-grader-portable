import { getSession,isOperator,request } from './api'
export function requireOperator(local=false){const s=getSession();if(!isOperator(s))throw new Error('仅管理员或本机工作台可管理系统。');if(local&&s?.cloud_mode!==false)throw new Error('此操作只允许在本机模式执行。')}
export function localManagement(){return isOperator(getSession())&&getSession()?.cloud_mode===false}
export function desktopManagement(){return localManagement()&&getSession()?.required===false}
function requireDesktop(){requireOperator(true);if(!desktopManagement())throw new Error('网页认证模式不开放此桌面本机操作。')}
export function operatorRequest<T>(path:string,method='GET',body?:unknown,local=false){requireOperator(local);return request<T>(path,method,body)}
export type User={username:string;role:string;enabled:boolean;created_at:number}
function requireAdmin(){if(getSession()?.role!=='admin')throw new Error('仅管理员可管理教师账号。')}
export function users(){requireAdmin();return request<{users:User[]}>('/api/auth/users')}
export function userAction(action:'create'|'reset-password'|'disable'|'enable',username:string,password='',confirmed=false){requireAdmin();if(!confirmed)throw new Error('请先确认账号操作。');return request('/api/auth/users/'+action,'POST',{username,...(['create','reset-password'].includes(action)?{password}:{})})}
export type Preset={id:string;label:string;base_url?:string;model:string;provider_label?:string;note?:string;user_defined?:boolean;experimental?:boolean}
export type Configuration={model_preset:string;base_url:string;model:string;model_presets:Preset[];server_model_selection_enabled?:boolean;api_key_configured:boolean;api_key_persisted?:boolean;api_key_session_only?:boolean;network_addresses?:string[];service_port?:number;mobile_token_active?:boolean}
export type Pair={preset_a:string;preset_b:string;configured_a:boolean;configured_b:boolean;available_models:Preset[];label_a?:string;label_b?:string}
export function settingsBody(cloud:boolean,preset:string,base:string,model:string,key:string,remember:boolean){if(!preset)throw new Error('请选择模型预设。');return cloud?{preset_id:preset}:{preset_id:preset,base_url:base,model,api_key:key,remember_key:remember}}
export function saveSettings(preset:string,base:string,model:string,key:string,remember:boolean,confirmed:boolean){requireOperator();if(!confirmed)throw new Error('请确认配置修改。');const cloud=getSession()?.cloud_mode;if(typeof cloud!=='boolean')throw new Error('无法确认运行模式，请重新登录。');return operatorRequest('/api/settings','POST',settingsBody(cloud,preset,base,model,key,remember),!cloud)}
export function savePair(side:'A'|'B',preset:string,key:string,confirmed:boolean){requireOperator();if(typeof getSession()?.cloud_mode!=='boolean')throw new Error('无法确认运行模式，请重新登录。');if(!confirmed||!preset)throw new Error('请选择模型并确认修改。');return operatorRequest('/api/dual-review/configuration','POST',{side,preset_id:preset,api_key:key})}
export function saveCustom(preset_id:string,label:string,base_url:string,model:string,note:string,confirmed:boolean){requireDesktop();if(!confirmed||!label.trim()||!base_url.trim()||!model.trim())throw new Error('请填写并确认自定义预设。');return operatorRequest('/api/custom-models/save','POST',{preset_id,label,base_url,model,note},true)}
export function fetchCustomModels(base_url:string,api_key:string){requireDesktop();if(!base_url.trim())throw new Error('请填写接口地址。');return operatorRequest<{models:string[]}>('/api/custom-models/list','POST',{base_url,api_key,confirm_send:true},true)}
export function deleteCustom(p:Preset,confirmation:string){requireDesktop();if(!p.user_defined||confirmation!==p.label)throw new Error('请核对并输入自定义预设名称。');return operatorRequest('/api/custom-models/delete','POST',{preset_id:p.id},true)}
export type Backup={filename:string;created_at:string;bytes:number}
export type Retention={retention_days:number;total_original_images:number;hypothetical_eligible_images:number;held_images:number;deletion_performed:boolean;held_reason?:string}
export function restoreBackup(filename:string,typed:string,consent:boolean){requireOperator(true);if(!consent||!filename||typed!==filename||/[\\/]/.test(filename)||!filename.endsWith('.zip'))throw new Error('请核对并输入完整备份文件名。');return operatorRequest<{message:string;safety_backup:string}>('/api/backup/restore','POST',{filename},true)}
export function retentionPreview(days:number){requireOperator(true);if(!Number.isInteger(days)||days<0||days>3650)throw new Error('保留天数须为0—3650的整数。');return operatorRequest<Retention>('/api/retention/preview','POST',{retention_days:days,dry_run:true},true)}
export function serviceAction(action:'restart'|'shutdown',typed:string,consent:boolean){requireDesktop();if(!consent||typed!==(action==='restart'?'重启':'停止'))throw new Error('请明确确认本机服务操作。');return operatorRequest('/api/'+action,'POST',{},true)}
