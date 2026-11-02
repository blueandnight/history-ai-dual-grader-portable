import {describe,it,expect} from 'vitest'
import {requireCleared,type DeleteResult} from './answerManagement'
const cleared:DeleteResult={ok:true,verified_remaining:{subjective:0,objective:0,jobs:0,batches:0,identities:0,links:0}}
describe('清空结果使用服务端实际核验',()=>{
 it('两类答卷和关联均已清零才允许显示成功',()=>{expect(()=>requireCleared(cleared)).not.toThrow()})
 it('仅主观题为零、有客观题或身份残留时拒绝显示成功',()=>{
  for(const field of ['objective','identities','jobs','links'] as const)expect(()=>requireCleared({...cleared,verified_remaining:{...cleared.verified_remaining!,[field]:82}})).toThrow('尚未核实')
 })
 it('旧响应、缺少核验字段或失败响应不当作成功',()=>{
  for(const value of [{ok:true},{},{...cleared,ok:false}])expect(()=>requireCleared(value)).toThrow('尚未核实')
 })
})
