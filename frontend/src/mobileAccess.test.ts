import {afterEach,expect,it,vi} from 'vitest'
import {setSession} from './api'
import {mobileAddresses,createMobileAccess,revokeMobileAccess,readMobileStatus} from './mobileAccess'
function session(cloud=false,teacher=false){setSession({required:true,authenticated:true,role:teacher?'teacher':'admin',cloud_mode:cloud,csrf_token:'SYNTHETIC'});vi.stubGlobal('window',{location:{origin:'http://127.0.0.1:8765'}});vi.stubGlobal('fetch',vi.fn(async()=>new Response('{}')))}
afterEach(()=>{setSession(null);vi.unstubAllGlobals()})
it('uses local IPv4/port and preserves connection labels, rejecting injected/invalid hosts',()=>{
 session();const s={service_port:8765,network_interfaces:[{address:'192.168.1.2',connection_label:'Wi-Fi'},{address:'192.168.1.2'},{address:'999.0.0.1'},{address:'x.test/?token=bad'}]}
 expect(mobileAddresses(s,'http://127.0.0.1:8765','synthetic+token')).toEqual([{label:'Wi-Fi',url:'http://192.168.1.2:8765/?access_token=synthetic%2Btoken'}])
 expect(mobileAddresses({...s,service_port:0},'http://localhost')).toEqual([])
})
it('cloud mode uses only the current service origin and clears old query/hash',()=>{
 session(true);expect(mobileAddresses({network_addresses:['192.168.1.2']},'https://school.example/next/?old=x#route','SYNTHETIC')).toEqual([{label:'当前服务地址',url:'https://school.example/?access_token=SYNTHETIC'}])
 expect(mobileAddresses({},'javascript:alert(1)','SYNTHETIC')).toEqual([])
})
it('keeps refresh read-only and requires explicit generation consent/CSRF',async()=>{
 session();await readMobileStatus();expect(fetch).toHaveBeenCalledTimes(1);expect(()=>createMobileAccess(false)).toThrow();await createMobileAccess(true);expect(fetch).toHaveBeenLastCalledWith('/api/mobile-token',expect.objectContaining({method:'POST',headers:expect.objectContaining({'X-CSRF-Token':'SYNTHETIC'})}));await revokeMobileAccess();expect(fetch).toHaveBeenLastCalledWith('/api/mobile-token/revoke',expect.objectContaining({method:'POST'}))
})
it('teacher and unknown sessions cannot read addresses or issue/revoke tokens',()=>{
 session(false,true);expect(()=>readMobileStatus()).toThrow();expect(()=>createMobileAccess(true)).toThrow();expect(()=>revokeMobileAccess()).toThrow();expect(fetch).not.toHaveBeenCalled();setSession(null);expect(mobileAddresses({service_port:8765,network_addresses:['192.168.1.2']},'http://localhost')).toEqual([])
})
