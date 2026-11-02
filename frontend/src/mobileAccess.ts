import {getSession} from './api'
import {operatorRequest} from './systemManagement'
export type MobileStatus={network_addresses?:string[];network_interfaces?:{address:string;connection_label?:string;recommended?:boolean}[];service_port?:number|null;mobile_token_active?:boolean}
export function readMobileStatus(){return operatorRequest<MobileStatus>('/api/status?refresh=1')}
export function createMobileAccess(consent:boolean){if(!consent)throw new Error('请先确认生成手机访问码');return operatorRequest<{token:string;expires_at:number}>('/api/mobile-token','POST',{})}
export function revokeMobileAccess(){return operatorRequest('/api/mobile-token/revoke','POST',{})}
export function mobileAddresses(status:MobileStatus,origin:string,token=''){
 const cloud=getSession()?.cloud_mode
 if(typeof cloud!=='boolean')return []
 if(cloud){const u=new URL(origin);if(!['http:','https:'].includes(u.protocol)||u.username||u.password)return [];u.pathname='/';u.search='';u.hash='';if(token)u.searchParams.set('access_token',token);return [{label:'当前服务地址',url:u.href}]}
 if(!Number.isInteger(status.service_port)||!status.service_port||status.service_port<1||status.service_port>65535)return []
 const records=status.network_interfaces??status.network_addresses?.map(address=>({address,connection_label:'局域网'}))??[]
 const seen=new Set<string>()
 return records.filter(r=>{const valid=/^\d{1,3}(\.\d{1,3}){3}$/.test(r.address)&&r.address.split('.').every(n=>Number(n)<=255)&&!seen.has(r.address);if(valid)seen.add(r.address);return valid}).map(r=>({label:r.connection_label||'局域网',url:`http://${r.address}:${status.service_port}/${token?'?access_token='+encodeURIComponent(token):''}`}))
}
